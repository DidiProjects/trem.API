"""
Testes do AirlineApiClient — cliente HTTP para a API aérea na rede Docker.

Usa httpx.MockTransport: exercita o código real do httpx (headers, base_url,
raise_for_status) sem abrir socket.
"""
from unittest.mock import patch

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.infrastructure.http.airline_client import AirlineApiClient, AirlineApiError

BASE_URL = "http://airline-api:8000"


def _settings_with(**overrides) -> Settings:
    data = get_settings().model_dump()
    data.update(overrides)
    return Settings(**data)


@pytest.fixture
def configured():
    settings = _settings_with(
        AIRLINE_API_URL=BASE_URL, AIRLINE_API_KEY="chave-secreta", AIRLINE_API_TIMEOUT=5
    )
    with patch(
        "app.infrastructure.http.airline_client.get_settings", return_value=settings
    ):
        yield settings


def _client_with_handler(handler):
    """Devolve um AirlineApiClient cujo httpx.AsyncClient usa o handler dado."""
    client = AirlineApiClient()
    transport = httpx.MockTransport(handler)
    original = client._client

    def _patched():
        real = original()
        real._transport = transport
        return real

    client._client = _patched
    return client


class TestConstruction:
    def test_requires_airline_api_url(self):
        unconfigured = _settings_with(AIRLINE_API_URL=None)
        with patch(
            "app.infrastructure.http.airline_client.get_settings",
            return_value=unconfigured,
        ):
            with pytest.raises(AirlineApiError) as exc:
                AirlineApiClient()
            assert exc.value.status_code == 503

    def test_strips_trailing_slash_from_base_url(self):
        settings = _settings_with(AIRLINE_API_URL=f"{BASE_URL}/")
        with patch(
            "app.infrastructure.http.airline_client.get_settings", return_value=settings
        ):
            assert AirlineApiClient()._base_url == BASE_URL

    def test_sets_api_key_header(self, configured):
        assert AirlineApiClient()._headers["X-API-Key"] == "chave-secreta"

    def test_omits_header_without_api_key(self):
        settings = _settings_with(AIRLINE_API_URL=BASE_URL, AIRLINE_API_KEY=None)
        with patch(
            "app.infrastructure.http.airline_client.get_settings", return_value=settings
        ):
            assert "X-API-Key" not in AirlineApiClient()._headers


class TestHealthCheck:
    async def test_returns_true_on_200(self, configured):
        client = _client_with_handler(lambda req: httpx.Response(200, json={"ok": True}))
        assert await client.health_check() is True

    async def test_returns_false_on_500(self, configured):
        client = _client_with_handler(lambda req: httpx.Response(500))
        assert await client.health_check() is False

    async def test_returns_false_on_connection_error(self, configured):
        def boom(request):
            raise httpx.ConnectError("sem rota para o host", request=request)

        assert await _client_with_handler(boom).health_check() is False

    async def test_calls_health_path(self, configured):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            return httpx.Response(200)

        await _client_with_handler(handler).health_check()
        assert seen["path"] == "/health"


class TestGet:
    async def test_returns_parsed_json(self, configured):
        client = _client_with_handler(
            lambda req: httpx.Response(200, json={"voos": [1, 2, 3]})
        )
        assert await client.get("/voos") == {"voos": [1, 2, 3]}

    async def test_forwards_query_params(self, configured):
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={})

        await _client_with_handler(handler).get("/voos", params={"origem": "CNF"})
        assert seen["params"] == {"origem": "CNF"}

    async def test_sends_api_key_header(self, configured):
        seen = {}

        def handler(request):
            seen["key"] = request.headers.get("x-api-key")
            return httpx.Response(200, json={})

        await _client_with_handler(handler).get("/voos")
        assert seen["key"] == "chave-secreta"

    async def test_http_error_raises_airline_api_error(self, configured):
        client = _client_with_handler(lambda req: httpx.Response(404))
        with pytest.raises(AirlineApiError) as exc:
            await client.get("/inexistente")
        assert exc.value.status_code == 502

    async def test_server_error_is_not_retried(self, configured):
        """5xx é resposta válida do servidor — retry só vale para falha de rede."""
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503)

        with pytest.raises(AirlineApiError):
            await _client_with_handler(handler).get("/voos")
        assert len(calls) == 1


class TestPost:
    async def test_sends_json_body(self, configured):
        import json as jsonlib

        seen = {}

        def handler(request):
            seen["body"] = jsonlib.loads(request.content)
            return httpx.Response(200, json={"id": 1})

        await _client_with_handler(handler).post("/reservas", json={"voo": "AB123"})
        assert seen["body"] == {"voo": "AB123"}

    async def test_returns_parsed_json(self, configured):
        client = _client_with_handler(lambda req: httpx.Response(201, json={"id": 7}))
        assert await client.post("/reservas", json={}) == {"id": 7}


class TestRetryOnNetworkFailure:
    @pytest.fixture(autouse=True)
    def _no_sleep(self):
        """Neutraliza o backoff para o teste não levar 7 segundos."""
        async def instant(_seconds):
            return None

        with patch("asyncio.sleep", instant):
            yield

    async def test_retries_three_times_then_raises(self, configured):
        calls = []

        def handler(request):
            calls.append(1)
            raise httpx.ConnectError("recusado", request=request)

        with pytest.raises(AirlineApiError):
            await _client_with_handler(handler).get("/voos")
        assert len(calls) == 3

    async def test_succeeds_on_retry_after_transient_failure(self, configured):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) < 3:
                raise httpx.ConnectError("recusado", request=request)
            return httpx.Response(200, json={"ok": True})

        assert await _client_with_handler(handler).get("/voos") == {"ok": True}
        assert len(calls) == 3

    async def test_timeout_is_retried(self, configured):
        calls = []

        def handler(request):
            calls.append(1)
            raise httpx.ReadTimeout("estourou o tempo", request=request)

        with pytest.raises(AirlineApiError):
            await _client_with_handler(handler).get("/voos")
        assert len(calls) == 3


class TestAirlineApiError:
    def test_default_status_is_502(self):
        assert AirlineApiError("falhou").status_code == 502

    def test_message_is_prefixed(self):
        assert "Erro na API aérea" in AirlineApiError("falhou").message
