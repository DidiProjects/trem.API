"""
Testes do LlmClient — cliente HTTP para o llama-server do trem-llm.

Usa httpx.MockTransport, como test_airline_client.py.
"""
from unittest.mock import patch

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.infrastructure.http.llm_client import LlmApiError, LlmClient

BASE_URL = "http://trem-llm:8080"


def _settings_with(**overrides) -> Settings:
    data = get_settings().model_dump()
    data.update(overrides)
    return Settings(**data)


@pytest.fixture
def configured():
    settings = _settings_with(LLM_API_URL=BASE_URL, LLM_API_KEY="llm-key", LLM_TIMEOUT=5)
    with patch("app.infrastructure.http.llm_client.get_settings", return_value=settings):
        yield settings


def _client_with_handler(handler):
    client = LlmClient()
    transport = httpx.MockTransport(handler)
    original = client._client

    def _patched():
        real = original()
        real._transport = transport
        return real

    client._client = _patched
    return client


class TestConstruction:
    def test_requires_llm_api_url(self):
        with patch(
            "app.infrastructure.http.llm_client.get_settings",
            return_value=_settings_with(LLM_API_URL=None),
        ):
            with pytest.raises(LlmApiError) as exc:
                LlmClient()
            assert exc.value.status_code == 503

    def test_sets_bearer_header(self, configured):
        assert LlmClient()._headers["Authorization"] == "Bearer llm-key"

    def test_connect_fails_fast_read_waits_long(self, configured):
        timeout = LlmClient()._timeout
        assert timeout.connect == 5.0
        assert timeout.read == 5


class TestHealthCheck:
    async def test_true_on_200(self, configured):
        client = _client_with_handler(lambda r: httpx.Response(200, json={"status": "ok"}))
        assert await client.health_check() is True

    async def test_false_on_error(self, configured):
        def handler(request):
            raise httpx.ConnectError("refused")

        assert await _client_with_handler(handler).health_check() is False


class TestChat:
    async def test_returns_upstream_json(self, configured):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"choices": [], "usage": {}})

        result = await _client_with_handler(handler).chat({"messages": []})
        assert result == {"choices": [], "usage": {}}
        assert seen == {"url": f"{BASE_URL}/v1/chat/completions", "auth": "Bearer llm-key"}

    async def test_400_passes_upstream_message(self, configured):
        body = {"error": {"code": 400, "message": "'messages' is required"}}
        client = _client_with_handler(lambda r: httpx.Response(400, json=body))
        with pytest.raises(LlmApiError) as exc:
            await client.chat({})
        assert exc.value.status_code == 400
        assert exc.value.message == "'messages' is required"

    async def test_400_without_json_body(self, configured):
        client = _client_with_handler(lambda r: httpx.Response(400, text="bad"))
        with pytest.raises(LlmApiError) as exc:
            await client.chat({})
        assert exc.value.message == "Invalid request"

    @pytest.mark.parametrize("upstream", [401, 403, 500, 503])
    async def test_other_errors_become_502(self, configured, upstream):
        client = _client_with_handler(lambda r: httpx.Response(upstream, text="secret body"))
        with pytest.raises(LlmApiError) as exc:
            await client.chat({})
        assert exc.value.status_code == 502
        assert "secret body" not in exc.value.message

    @pytest.mark.parametrize(
        "error, expected",
        [
            (httpx.ConnectError("refused"), 503),
            (httpx.ConnectTimeout("slow connect"), 503),
            (httpx.ReadTimeout("slow model"), 504),
        ],
    )
    async def test_transport_errors(self, configured, error, expected):
        def handler(request):
            raise error

        with pytest.raises(LlmApiError) as exc:
            await _client_with_handler(handler).chat({})
        assert exc.value.status_code == expected

    async def test_does_not_retry(self, configured):
        calls = []

        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("slow model")

        with pytest.raises(LlmApiError):
            await _client_with_handler(handler).chat({})
        assert len(calls) == 1


class TestIsSleeping:
    @pytest.mark.parametrize("sleeping", [True, False])
    async def test_reads_props(self, configured, sleeping):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"is_sleeping": sleeping})

        assert await _client_with_handler(handler).is_sleeping() is sleeping
        assert seen == {"path": "/props", "auth": "Bearer llm-key"}

    async def test_missing_field_means_awake(self, configured):
        client = _client_with_handler(lambda r: httpx.Response(200, json={}))
        assert await client.is_sleeping() is False

    async def test_connect_error_is_503(self, configured):
        def handler(request):
            raise httpx.ConnectError("refused")

        with pytest.raises(LlmApiError) as exc:
            await _client_with_handler(handler).is_sleeping()
        assert exc.value.status_code == 503

    async def test_upstream_error_is_502(self, configured):
        client = _client_with_handler(lambda r: httpx.Response(500, text="secret body"))
        with pytest.raises(LlmApiError) as exc:
            await client.is_sleeping()
        assert exc.value.status_code == 502
        assert "secret body" not in exc.value.message
