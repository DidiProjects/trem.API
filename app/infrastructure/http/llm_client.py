from typing import Any

import httpx

from app.core.config import get_settings
from app.core.exceptions import AppException


class LlmApiError(AppException):
    # Messages never carry upstream bodies: prompts and completions are personal data.
    def __init__(self, detail: str, status_code: int = 502):
        super().__init__(detail, status_code)


class LlmClient:
    """
    Client for the llama-server in the trem-llm container (internal Docker
    network). No retry: repeating an inference that takes minutes only doubles
    the wait and holds the single slot.
    """

    def __init__(self):
        settings = get_settings()
        if not settings.LLM_API_URL:
            raise LlmApiError("LLM_API_URL is not configured", status_code=503)
        self._base_url = settings.LLM_API_URL.rstrip("/")
        self._headers = {}
        if settings.LLM_API_KEY:
            self._headers["Authorization"] = f"Bearer {settings.LLM_API_KEY}"
        self._timeout = httpx.Timeout(settings.LLM_TIMEOUT, connect=5.0)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout,
        )

    async def health_check(self) -> bool:
        try:
            async with self._client() as client:
                resp = await client.get("/health")
                return resp.status_code == 200
        except Exception:
            return False

    async def is_sleeping(self) -> bool:
        # /props neither wakes the model nor resets its idle timer.
        try:
            async with self._client() as client:
                resp = await client.get("/props")
        except httpx.RequestError:
            raise LlmApiError("LLM unavailable", status_code=503)
        if resp.status_code != 200:
            raise LlmApiError(f"LLM error ({resp.status_code})", status_code=502)
        return bool(resp.json().get("is_sleeping"))

    async def chat(self, body: dict) -> Any:
        try:
            async with self._client() as client:
                resp = await client.post("/v1/chat/completions", json=body)
        except httpx.ConnectTimeout:
            raise LlmApiError("LLM unavailable", status_code=503)
        except httpx.TimeoutException:
            raise LlmApiError("LLM timed out", status_code=504)
        except httpx.RequestError:
            raise LlmApiError("LLM unavailable", status_code=503)

        if resp.status_code == 400:
            raise LlmApiError(_upstream_message(resp), status_code=400)
        if resp.status_code != 200:
            # 401/403 means the internal key is wrong: a config problem, not the caller's.
            raise LlmApiError(f"LLM error ({resp.status_code})", status_code=502)
        return resp.json()


def _upstream_message(resp: httpx.Response) -> str:
    try:
        error = resp.json().get("error", {})
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    except ValueError:
        pass
    return "Invalid request"
