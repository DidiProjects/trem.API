import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional

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

    def admit(self, max_waiting: int) -> None:
        if self._waiting >= max_waiting:
            raise HTTPException(
                status_code=503,
                detail="LLM busy, retry later",
                headers={"Retry-After": "60"},
            )
        self._waiting += 1

    @asynccontextmanager
    async def admitted(self):
        """The slot for a caller already counted by `admit`."""
        try:
            async with self._slot:
                yield
        finally:
            self._waiting -= 1

    @asynccontextmanager
    async def turn(self, max_waiting: int):
        self.admit(max_waiting)
        async with self.admitted():
            yield


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

    async def wait(self) -> None:
        if self._task is not None:
            # Shielded: a cancelled job must not abort the wake other callers rely on.
            await asyncio.shield(self._task)

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


def _log(kind: str, principal_id: str, status: int, started: float, result) -> None:
    # Metrics only: bodies hold CVs and job posts, which are personal data.
    prompt_tokens, cached_tokens, completion_tokens = _usage(result)
    logger.info(
        "llm %s principal=%s status=%d duration_ms=%d prompt_tokens=%s "
        "cached_tokens=%s completion_tokens=%s",
        kind,
        principal_id,
        status,
        int((time.monotonic() - started) * 1000),
        prompt_tokens,
        cached_tokens,
        completion_tokens,
    )


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class LlmJob:
    id: str
    owner_id: str
    status: JobStatus = JobStatus.QUEUED
    result: Optional[dict] = None
    error: Optional[str] = None
    finished_at: Optional[float] = None

    @property
    def finished(self) -> bool:
        return self.status in (JobStatus.DONE, JobStatus.FAILED)

    def body(self) -> dict:
        body = {"job_id": self.id, "status": self.status.value}
        if self.result is not None:
            body["result"] = self.result
        if self.error is not None:
            body["error"] = self.error
        return body


class LlmJobs:
    """
    Inference that outlives the request: Cloudflare cuts responses at 100 s
    and nginx at 60 s, so callers on the public URL submit and poll. Jobs share
    the queue and the warmup with the synchronous endpoint, and live in memory
    like them (single process). Results hold personal data, so finished jobs
    are dropped after `LLM_JOB_TTL_SECONDS`.
    """

    def __init__(self):
        self._jobs: dict[str, LlmJob] = {}
        self._tasks: set[asyncio.Task] = set()  # kept so running jobs are not garbage collected

    def submit(self, owner_id: str, body: dict, settings) -> LlmJob:
        self.sweep(settings.LLM_JOB_TTL_SECONDS)
        _queue.admit(settings.LLM_QUEUE_SIZE)
        job = LlmJob(id=uuid.uuid4().hex, owner_id=owner_id)
        self._jobs[job.id] = job
        task = asyncio.create_task(self._run(job, _sanitize(body, settings.LLM_MAX_TOKENS)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: LlmJob, body: dict) -> None:
        started = time.monotonic()
        status = 500
        try:
            async with _queue.admitted():
                job.status = JobStatus.RUNNING
                client = LlmClient()
                if _warmup.in_progress or await client.is_sleeping():
                    _warmup.start()
                    await _warmup.wait()
                job.result = await client.chat(body)
            status = 200
        except LlmApiError as e:
            status = e.status_code
            job.error = e.message
        except Exception as e:
            logger.error("llm job crashed error=%s", type(e).__name__)
            job.error = "LLM job failed"
        finally:
            job.finished_at = time.time()
            job.status = JobStatus.DONE if status == 200 else JobStatus.FAILED
            _log("job", job.owner_id, status, started, job.result)

    def get(self, owner_id: str, job_id: str, ttl_seconds: int) -> LlmJob:
        self.sweep(ttl_seconds)
        job = self._jobs.get(job_id)
        if job is None or job.owner_id != owner_id:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    def sweep(self, ttl_seconds: int) -> None:
        cutoff = time.time() - ttl_seconds
        for job in [j for j in self._jobs.values() if j.finished and j.finished_at < cutoff]:
            del self._jobs[job.id]


_jobs = LlmJobs()


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
        _log("request", principal.id, status, started, result)


@router.post("/jobs", status_code=202)
async def submit_job(
    body: dict = Body(...),
    principal: Principal = Depends(get_current_principal),
):
    """
    Same body as /v1/chat/completions, answered at once with a `job_id` to poll.
    A sleeping model is woken inside the job instead of answering 503.
    """
    try:
        LlmClient()  # fails fast with 503 when LLM_API_URL is not configured
    except LlmApiError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    return _jobs.submit(principal.id, body, get_settings()).body()


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    principal: Principal = Depends(get_current_principal),
):
    """Job state: queued, running, done (with `result`) or failed (with `error`)."""
    return _jobs.get(principal.id, job_id, get_settings().LLM_JOB_TTL_SECONDS).body()


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
