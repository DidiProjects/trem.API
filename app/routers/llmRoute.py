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

_WAKE_BODY = {"messages": [{"role": "user", "content": "ping"}], "max_tokens": 1, "stream": False}


class LlmWarmup:
    """
    Answers "warming up" instead of holding the connection while trem-llm
    reloads after --sleep-idle-seconds, which otherwise ends in 504/524 and
    blind retries. llama-server exposes the sleeping state but not the load
    progress, so eta/progress are estimates from the measured reload time.
    """

    def __init__(self):
        self._since: float | None = None
        self._task: asyncio.Task | None = None  # kept so the task is not garbage collected

    @property
    def in_progress(self) -> bool:
        return self._since is not None

    def start(self) -> None:
        if self.in_progress:
            return
        self._since = time.monotonic()
        self._task = asyncio.create_task(self._wake())

    async def _wake(self) -> None:
        try:
            await LlmClient().chat(_WAKE_BODY)
        except LlmApiError as e:
            logger.warning("llm warmup failed status=%d", e.status_code)
        finally:
            self._since = None

    def response(self, expected_seconds: int) -> JSONResponse:
        elapsed = time.monotonic() - (self._since or time.monotonic())
        eta = int(max(expected_seconds - elapsed, 15))  # never tell the client "0"
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": str(eta)},
            content={
                "status": "warming_up",
                "detail": "Model is waking up, retry after the indicated time",
                "elapsed_seconds": int(elapsed),
                "eta_seconds": eta,
                "progress": round(min(elapsed / expected_seconds, 0.95), 2),
            },
        )


_warmup = LlmWarmup()


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
        client = LlmClient()
        if _warmup.in_progress or await client.is_sleeping():
            _warmup.start()
            status = 503
            return _warmup.response(settings.LLM_WARMUP_SECONDS)
        async with _queue.turn(settings.LLM_QUEUE_SIZE):
            result = await client.chat(_sanitize(body, settings.LLM_MAX_TOKENS))
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
        client = LlmClient()
        if not await client.health_check():
            raise HTTPException(status_code=503, detail="LLM unavailable")
        if _warmup.in_progress:
            return {"status": "warming_up"}
        return {"status": "sleeping" if await client.is_sleeping() else "healthy"}
    except LlmApiError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
