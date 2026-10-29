import asyncio
from typing import Any, Optional

import httpx
import websockets

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

    def stream_url(self, path: str) -> str:
        return self._base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + path

    def stream_headers(self, owner_id: str) -> dict:
        return {"X-Internal-Key": self._key, "X-Owner-Id": owner_id}


async def relay(websocket, upstream_url: str, headers: dict) -> None:
    """
    Pumps a browser WebSocket to the orchestrator and back until either side closes. Frames are
    the noVNC stream (screen out, keyboard and mouse in), kept binary under its subprotocol.
    """
    wanted = websocket.scope.get("subprotocols") or []
    subprotocol = "binary" if "binary" in wanted else None
    try:
        upstream = await websockets.connect(
            upstream_url,
            additional_headers=headers,
            subprotocols=[subprotocol] if subprotocol else None,
            max_size=None,
            open_timeout=10,
        )
    except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException):
        await websocket.close(code=1011)
        return
    await websocket.accept(subprotocol=subprotocol)

    async def browser_to_upstream():
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            await upstream.send(data if data is not None else message.get("text"))

    async def upstream_to_browser():
        async for data in upstream:
            if isinstance(data, bytes):
                await websocket.send_bytes(data)
            else:
                await websocket.send_text(data)

    tasks = [asyncio.create_task(browser_to_upstream()), asyncio.create_task(upstream_to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await upstream.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass  # the browser already closed it
