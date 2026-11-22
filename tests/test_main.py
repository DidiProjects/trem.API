"""
Testes do app FastAPI em si: endpoints de infraestrutura, montagem dos
routers e integridade do schema OpenAPI.
"""
import pytest

from app.main import app


class TestHealthCheck:
    def test_returns_healthy(self, client_no_auth):
        response = client_no_auth.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_is_public(self, client_no_auth):
        """O healthcheck do Docker chama sem credencial — não pode exigir auth."""
        assert client_no_auth.get("/health").status_code == 200


class TestRoot:
    def test_sends_to_the_documentation(self, client_no_auth):
        response = client_no_auth.get("/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/docs"

    def test_documentation_is_served(self, client_no_auth):
        assert client_no_auth.get("/docs").status_code == 200

    @pytest.mark.parametrize("path", ["/config", "/static/script.js"])
    def test_the_playground_is_gone(self, client_no_auth, path):
        assert client_no_auth.get(path).status_code == 404


class TestOpenApiSchema:
    def test_schema_builds(self):
        assert app.openapi()["info"]["title"] == "trem.API"

    @pytest.mark.parametrize(
        "path",
        [
            "/me",
            "/me/api-keys",
            "/admin/applications",
            "/hub/services",
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
        assert client_no_auth.get("/support/feedback").status_code == 405


class TestOwnLoginIsGone:
    @pytest.mark.parametrize("path", ["/auth/login", "/auth/refresh", "/auth/register", "/users", "/.well-known/jwks.json"])
    def test_route_is_not_registered(self, path):
        assert path not in app.openapi()["paths"]
