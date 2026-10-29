from typing import Any, Optional

import httpx

from app.core.config import get_settings
from app.core.exceptions import AppException


class ScraperApiError(AppException):
    def __init__(self, detail: str, status_code: int = 502):
        super().__init__(detail, status_code)


class ScraperClient:
    """
    Client for the LinkedIn scraper orchestrator on the internal Docker network.

    The orchestrator scopes every request to X-Owner-Id and trusts it only next to the internal
    key, so the owner always comes from the authenticated principal, never from the caller.
    No retry: creating a job or queueing a run twice is not harmless.
    """

    def __init__(self):
        settings = get_settings()
        if not settings.SCRAPER_API_URL or not settings.SCRAPER_API_KEY:
            raise ScraperApiError("Scraper service is not configured", status_code=503)
        self._base_url = settings.SCRAPER_API_URL.rstrip("/")
        self._key = settings.SCRAPER_API_KEY
        self._timeout = httpx.Timeout(settings.SCRAPER_TIMEOUT, connect=5.0)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)

    async def request(
        self,
        method: str,
        path: str,
        owner_id: str,
        *,
        json: Any = None,
        content: Optional[bytes] = None,
        params: Optional[dict] = None,
    ) -> httpx.Response:
        headers = {"X-Internal-Key": self._key, "X-Owner-Id": owner_id}
        if content is not None:
            headers["Content-Type"] = "application/octet-stream"
        try:
            async with self._client() as client:
                return await client.request(
                    method, path, headers=headers, json=json, content=content, params=params
                )
        except httpx.TimeoutException:
            raise ScraperApiError("Scraper service timed out", status_code=504)
        except httpx.RequestError:
            raise ScraperApiError("Scraper service unavailable", status_code=503)
