import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.api.v1.dependencies import get_current_principal
from app.core.config import get_settings
from app.domain.entities.api_client import Principal
from app.infrastructure.http.llm_client import LlmApiError, LlmClient

logger = logging.getLogger(__name__)

router = APIRouter()


class LlmQueue:
    """
    trem-llm runs with --parallel 1, so requests take turns here instead of
    piling up upstream. In-memory: valid only while uvicorn runs one worker.

    Known limitation: Starlette does not cancel the handler when the client
    disconnects, so an abandoned inference still holds the slot until it ends.
    """

    def __init__(self):
        self._slot = asyncio.Semaphore(1)
        self._waiting = 0

    @asynccontextmanager
    async def turn(self, max_waiting: int):
        if self._waiting >= max_waiting:
            raise HTTPException(
                status_code=503,
                detail="LLM busy, retry later",
                headers={"Retry-After": "60"},
            )
        self._waiting += 1
        try:
            async with self._slot:
                yield
        finally:
            self._waiting -= 1


_queue = LlmQueue()


def _sanitize(body: dict, max_tokens: int) -> dict:
    body = dict(body)
    body["stream"] = False
    body.pop("model", None)
    requested = body.pop("max_completion_tokens", None) or body.get("max_tokens")
    if isinstance(requested, int) and 0 < requested < max_tokens:
        body["max_tokens"] = requested
    else:
        body["max_tokens"] = max_tokens
    return body


def _usage(result) -> tuple:
    usage = result.get("usage") if isinstance(result, dict) else None
    if not isinstance(usage, dict):
        return None, None, None
    details = usage.get("prompt_tokens_details") or {}
    return (
        usage.get("prompt_tokens"),
        details.get("cached_tokens") if isinstance(details, dict) else None,
        usage.get("completion_tokens"),
    )


@router.post("/v1/chat/completions")
async def chat_completions(
    body: dict = Body(...),
    principal: Principal = Depends(get_current_principal),
):
    """
    OpenAI-compatible chat completion, without streaming. `model` is ignored
    and `max_tokens` is capped by the server.
    """
    settings = get_settings()
    started = time.monotonic()
    status = 500
    result = None
    try:
        async with _queue.turn(settings.LLM_QUEUE_SIZE):
            result = await LlmClient().chat(_sanitize(body, settings.LLM_MAX_TOKENS))
        status = 200
        return JSONResponse(content=result)
    except LlmApiError as e:
        status = e.status_code
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except HTTPException as e:
        status = e.status_code
        raise
    finally:
        # Metrics only: bodies hold CVs and job posts, which are personal data.
        prompt_tokens, cached_tokens, completion_tokens = _usage(result)
        logger.info(
            "llm request principal=%s status=%d duration_ms=%d prompt_tokens=%s "
            "cached_tokens=%s completion_tokens=%s",
            principal.id,
            status,
            int((time.monotonic() - started) * 1000),
            prompt_tokens,
            cached_tokens,
            completion_tokens,
        )


@router.get("/health")
async def llm_health(_: Principal = Depends(get_current_principal)):
    try:
        healthy = await LlmClient().health_check()
    except LlmApiError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    if not healthy:
        raise HTTPException(status_code=503, detail="LLM unavailable")
    return {"status": "healthy"}
