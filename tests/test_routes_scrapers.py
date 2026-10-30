"""
/scrapers/linkedin: authorization, owner propagation and error mapping, with the orchestrator
replaced by httpx.MockTransport.
"""
import json
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.v1.dependencies import get_current_principal
from app.core.config import Settings, get_settings
from app.domain.entities.api_client import Principal, PrincipalKind
from app.infrastructure.database.connection import get_db
from app.infrastructure.http.scraper_client import ScraperClient
from app.main import app
from tests.conftest import mock_get_db

BASE_URL = "http://linkedinho:8080"
INTERNAL_KEY = "internal-key-" + "x" * 32
USER_ID = "00000000-0000-0000-0000-0000000000aa"
JOB_ID = "a" * 32
PREFIX = "/scrapers/linkedin"


class Orchestrator:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.handler = lambda request: httpx.Response(200, json={"ok": True})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture
def orchestrator(monkeypatch):
    fake = Orchestrator()
    transport = httpx.MockTransport(fake)
    original = ScraperClient._client

    def _client(self):
        real = original(self)
        real._transport = transport
        return real

    monkeypatch.setattr(ScraperClient, "_client", _client)
    data = get_settings().model_dump() | {"SCRAPER_API_URL": BASE_URL, "SCRAPER_API_KEY": INTERNAL_KEY}
    with patch("app.infrastructure.http.scraper_client.get_settings", return_value=Settings(**data)):
        yield fake


def _as(principal: Principal):
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_db] = mock_get_db
    return TestClient(app)


def _principal(profiles=("linkedin_scraper",), user_id=USER_ID, kind=PrincipalKind.CLIENT):
    return Principal(
        id="client-1", display_name="k", profile_names=frozenset(profiles), kind=kind, user_id=user_id
    )


@pytest.fixture
def client(orchestrator):
    yield _as(_principal())
    app.dependency_overrides.clear()


ROUTES = [
    ("get", f"{PREFIX}/jobs", {}),
    ("post", f"{PREFIX}/jobs", {"json": {"name": "x"}}),
    ("get", f"{PREFIX}/jobs/{JOB_ID}", {}),
    ("patch", f"{PREFIX}/jobs/{JOB_ID}", {"json": {"name": "x"}}),
    ("delete", f"{PREFIX}/jobs/{JOB_ID}", {}),
    ("put", f"{PREFIX}/jobs/{JOB_ID}/cv", {"files": {"file": ("cv.docx", b"PK\x03\x04", "application/octet-stream")}}),
    ("post", f"{PREFIX}/jobs/{JOB_ID}/pause", {}),
    ("post", f"{PREFIX}/jobs/{JOB_ID}/resume", {}),
    ("post", f"{PREFIX}/jobs/{JOB_ID}/run", {}),
    ("get", f"{PREFIX}/jobs/{JOB_ID}/matches", {}),
    ("get", f"{PREFIX}/session", {}),
    ("delete", f"{PREFIX}/session", {}),
]


class TestAuthorization:
    def test_requires_credentials(self, orchestrator):
        app.dependency_overrides[get_db] = mock_get_db
        try:
            http = TestClient(app)
            for method, path, kwargs in ROUTES:
                assert getattr(http, method)(path, **kwargs).status_code == 401, path
        finally:
            app.dependency_overrides.clear()
        assert orchestrator.requests == []

    @pytest.mark.parametrize("profiles", [("ai",), ("file_editor", "airline_company"), ()])
    def test_requires_linkedin_scraper_profile(self, orchestrator, profiles):
        http = _as(_principal(profiles))
        try:
            for method, path, kwargs in ROUTES:
                assert getattr(http, method)(path, **kwargs).status_code == 403, path
        finally:
            app.dependency_overrides.clear()
        assert orchestrator.requests == []

    def test_service_key_without_owner_is_403(self, orchestrator):
        http = _as(_principal(user_id=None))
        try:
            response = http.get(f"{PREFIX}/jobs")
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 403
        assert "owned by a user" in response.json()["detail"]
        assert orchestrator.requests == []

    def test_logged_in_user_is_their_own_owner(self, orchestrator):
        http = _as(_principal(kind=PrincipalKind.USER, user_id="u-42"))
        try:
            http.get(f"{PREFIX}/jobs")
        finally:
            app.dependency_overrides.clear()
        assert orchestrator.requests[-1].headers["x-owner-id"] == "u-42"


class TestForwarding:
    def test_owner_and_internal_key_come_from_the_server(self, client, orchestrator):
        client.get(f"{PREFIX}/jobs", headers={"X-Owner-Id": "someone-else", "X-Internal-Key": "forged"})
        sent = orchestrator.requests[-1]
        assert sent.headers["x-owner-id"] == USER_ID
        assert sent.headers["x-internal-key"] == INTERNAL_KEY
        assert "x-api-key" not in sent.headers

    @pytest.mark.parametrize("method,path,kwargs", ROUTES)
    def test_each_route_reaches_the_same_path_upstream(self, client, orchestrator, method, path, kwargs):
        orchestrator.handler = lambda request: (
            httpx.Response(204) if request.method in ("DELETE", "PUT") else httpx.Response(200, json={"ok": True})
        )
        assert getattr(client, method)(path, **kwargs).status_code < 300
        sent = orchestrator.requests[-1]
        assert sent.method == method.upper()
        assert sent.url.path == path.removeprefix(PREFIX)

    def test_job_body_is_passed_through(self, client, orchestrator):
        body = {"name": "UK", "schedule": {"timezone": "UTC", "days": ["mon"], "times": ["07:00"]}}
        orchestrator.handler = lambda request: httpx.Response(201, json={"id": JOB_ID})
        response = client.post(f"{PREFIX}/jobs", json=body)
        assert response.status_code == 201
        assert response.json() == {"id": JOB_ID}
        assert json.loads(orchestrator.requests[-1].content) == body

    def test_cv_goes_up_as_raw_bytes(self, client, orchestrator):
        orchestrator.handler = lambda request: httpx.Response(204)
        response = client.put(
            f"{PREFIX}/jobs/{JOB_ID}/cv", files={"file": ("cv.docx", b"PK\x03\x04docx-bytes", "application/x-docx")}
        )
        assert response.status_code == 204
        sent = orchestrator.requests[-1]
        assert sent.content == b"PK\x03\x04docx-bytes"
        assert sent.headers["content-type"] == "application/octet-stream"

    def test_oversized_cv_is_rejected_before_the_orchestrator(self, client, orchestrator):
        response = client.put(
            f"{PREFIX}/jobs/{JOB_ID}/cv", files={"file": ("cv.docx", b"x" * (2 * 1024 * 1024 + 1), "application/x-docx")}
        )
        assert response.status_code == 413
        assert orchestrator.requests == []

    def test_matches_query_is_validated_and_forwarded(self, client, orchestrator):
        client.get(f"{PREFIX}/jobs/{JOB_ID}/matches", params={"min_score": 70, "limit": 5})
        assert dict(orchestrator.requests[-1].url.params) == {"min_score": "70", "limit": "5"}
        assert client.get(f"{PREFIX}/jobs/{JOB_ID}/matches", params={"min_score": 101}).status_code == 422

    @pytest.mark.parametrize("job_id", ["..", "abc", "A" * 32, "a" * 31, "a" * 33, "%2e%2e", "session"])
    def test_only_job_ids_reach_the_upstream_path(self, client, orchestrator, job_id):
        response = client.get(f"{PREFIX}/jobs/{job_id}")
        assert response.status_code in (404, 422)
        assert orchestrator.requests == []


class TestErrors:
    @pytest.mark.parametrize("status", [400, 404, 409, 413, 422])
    def test_caller_errors_pass_through_with_detail(self, client, orchestrator, status):
        orchestrator.handler = lambda request: httpx.Response(status, json={"detail": "At most 3 jobs per user"})
        response = client.post(f"{PREFIX}/jobs", json={})
        assert response.status_code == status
        assert response.json()["detail"] == "At most 3 jobs per user"

    def test_validation_detail_list_passes_through(self, client, orchestrator):
        detail = [{"loc": ["body", "notify_to"], "msg": "value is not a valid email address", "type": "value_error"}]
        orchestrator.handler = lambda request: httpx.Response(422, json={"detail": detail})
        assert client.post(f"{PREFIX}/jobs", json={}).json()["detail"] == detail

    @pytest.mark.parametrize("status", [401, 500, 503])
    def test_orchestrator_failures_become_502_without_upstream_detail(self, client, orchestrator, status):
        orchestrator.handler = lambda request: httpx.Response(status, json={"detail": "Invalid internal key"})
        response = client.get(f"{PREFIX}/jobs")
        assert response.status_code == 502
        assert response.json()["detail"] == "Scraper service error"

    def test_html_error_page_does_not_break(self, client, orchestrator):
        orchestrator.handler = lambda request: httpx.Response(404, text="<html>nope</html>")
        response = client.get(f"{PREFIX}/jobs/{JOB_ID}")
        assert (response.status_code, response.json()["detail"]) == (404, "Request rejected")

    @pytest.mark.parametrize("error,expected", [
        (httpx.ConnectError("refused"), 503),
        (httpx.ReadTimeout("slow"), 504),
    ])
    def test_transport_errors(self, client, orchestrator, error, expected):
        def handler(request):
            raise error

        orchestrator.handler = handler
        assert client.get(f"{PREFIX}/jobs").status_code == expected

    def test_unconfigured_service_is_503(self, orchestrator):
        http = _as(_principal())
        try:
            with patch("app.infrastructure.http.scraper_client.get_settings", return_value=get_settings()):
                response = http.get(f"{PREFIX}/jobs")
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 503
        assert orchestrator.requests == []
