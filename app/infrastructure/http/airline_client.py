import logging
from typing import Any, Optional

import httpx

from app.core.config import get_settings
from app.core.exceptions import AppException

logger = logging.getLogger(__name__)

_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = [1.0, 2.0, 4.0]


class AirlineApiError(AppException):
    def __init__(self, detail: str, status_code: int = 502):
        super().__init__(f"Erro na API aérea: {detail}", status_code)


class AirlineApiClient:
    """
    Cliente HTTP assíncrono para a API de empresas aéreas
    na mesma rede Docker (http://airline-api:PORT).
    """

    def __init__(self):
        settings = get_settings()
        if not settings.AIRLINE_API_URL:
            raise AirlineApiError("AIRLINE_API_URL não configurado", status_code=503)
        self._base_url = settings.AIRLINE_API_URL.rstrip("/")
        self._headers = {}
        if settings.AIRLINE_API_KEY:
            self._headers["X-API-Key"] = settings.AIRLINE_API_KEY
        self._timeout = settings.AIRLINE_API_TIMEOUT

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

    async def get(self, path: str, params: Optional[dict] = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json: Optional[dict] = None) -> Any:
        return await self._request("POST", path, json=json)

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        import asyncio

        last_exc: Exception | None = None
        for attempt, backoff in enumerate(_RETRY_BACKOFF):
            try:
                async with self._client() as client:
                    resp = await client.request(method, path, **kwargs)
                    resp.raise_for_status()
                    return resp.json()
            except httpx.HTTPStatusError as e:
                raise AirlineApiError(
                    f"{method} {path} → {e.response.status_code}",
                    status_code=502,
                )
            except httpx.RequestError as e:
                last_exc = e
                logger.warning(
                    "AirlineApiClient: tentativa %d/%d falhou: %s",
                    attempt + 1,
                    _RETRY_ATTEMPTS,
                    str(e),
                )
                if attempt < _RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(backoff)

        raise AirlineApiError(f"Serviço indisponível após {_RETRY_ATTEMPTS} tentativas")
