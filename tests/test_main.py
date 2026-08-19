"""
Testes do app FastAPI em si: endpoints de infraestrutura, montagem dos
routers, middleware de cache e integridade do schema OpenAPI.
"""
import pytest

from app.core.config import get_settings
from app.main import app


class TestHealthCheck:
    def test_returns_healthy(self, client_no_auth):
        response = client_no_auth.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_is_public(self, client_no_auth):
        """O healthcheck do Docker chama sem credencial — não pode exigir auth."""
        assert client_no_auth.get("/health").status_code == 200


class TestConfigEndpoint:
    def test_exposes_api_url(self, client_no_auth):
        response = client_no_auth.get("/config")
        assert response.status_code == 200
        assert response.json()["api_url"] == get_settings().API_URL

    def test_does_not_leak_secrets(self, client_no_auth):
        """/config alimenta o front — só pode devolver a URL pública."""
        body = client_no_auth.get("/config").json()
        assert set(body.keys()) == {"api_url"}

    def test_is_hidden_from_schema(self):
        assert "/config" not in app.openapi()["paths"]


class TestRoot:
    def test_serves_html(self, client_no_auth):
        response = client_no_auth.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_injects_cache_busting_hashes(self, client_no_auth):
        """style.css/script.js recebem ?v=<hash> para invalidar cache no deploy."""
        html = client_no_auth.get("/").text
        assert "/static/style.css?v=" in html
        assert "/static/script.js?v=" in html

    def test_is_hidden_from_schema(self):
        assert "/" not in app.openapi()["paths"]


class TestStaticCacheControl:
    def test_versioned_asset_is_immutable(self, client_no_auth):
        response = client_no_auth.get("/static/style.css?v=abc123")
        assert response.status_code == 200
        assert "immutable" in response.headers["cache-control"]

    def test_unversioned_asset_has_short_ttl(self, client_no_auth):
        response = client_no_auth.get("/static/style.css")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "public, max-age=3600"

    def test_non_static_path_has_no_cache_header(self, client_no_auth):
        response = client_no_auth.get("/health")
        assert "cache-control" not in response.headers


class TestOpenApiSchema:
    def test_schema_builds(self):
        assert app.openapi()["info"]["title"] == "trem.API"

    @pytest.mark.parametrize(
        "path",
        [
            "/auth/login",
            "/auth/refresh",
            "/auth/change-password",
            "/auth/logout",
            "/users",
            "/users/{user_id}",
            "/pdf/split",
            "/movie/cut",
            "/audio/cut",
            "/image/convert",
            "/support/feedback",
            "/health",
        ],
    )
    def test_expected_route_is_registered(self, path):
        assert path in app.openapi()["paths"]

    def test_no_duplicate_operation_ids(self):
        """operationId repetido quebra geradores de client SDK."""
        operation_ids = [
            op["operationId"]
            for methods in app.openapi()["paths"].values()
            for op in methods.values()
            if isinstance(op, dict) and "operationId" in op
        ]
        assert len(operation_ids) == len(set(operation_ids))

    def test_routes_are_tagged(self):
        untagged = [
            f"{method.upper()} {path}"
            for path, methods in app.openapi()["paths"].items()
            for method, op in methods.items()
            if isinstance(op, dict) and not op.get("tags")
        ]
        assert untagged == []


class TestUnknownRoutes:
    def test_unknown_path_returns_404(self, client_no_auth):
        assert client_no_auth.get("/nao-existe").status_code == 404

    def test_wrong_method_returns_405(self, client_no_auth):
        assert client_no_auth.get("/auth/login").status_code == 405
