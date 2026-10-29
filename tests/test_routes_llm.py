"""
Testes das rotas /llm, com o llama-server substituído por httpx.MockTransport.
"""
import asyncio
import json
import logging
import time
from unittest.mock import patch

import httpx
import pytest

from app.api.v1.dependencies import (
    authenticated_user_or_none,
    get_current_principal,
    get_current_user,
)
from app.core.config import Settings, get_settings
from app.domain.entities.api_client import Principal
from app.infrastructure.database.connection import get_db
from app.infrastructure.http.llm_client import LlmClient
from app.main import app
from app.routers import llmRoute
from tests.conftest import _user_file_editor, make_mock_user, mock_get_db

BASE_URL = "http://trem-llm:8080"
SENTINEL = "SENTINEL-personal-data-7f3a"

COMPLETION = {
    "choices": [{"message": {"role": "assistant", "content": "OK"}}],
    "usage": {
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "prompt_tokens_details": {"cached_tokens": 8},
    },
    "timings": {"predicted_per_second": 13.0},
}

CHAT = {"messages": [{"role": "user", "content": "Reply with OK."}]}


class Upstream:
    """/props is answered apart, so `requests` holds only chat and health calls."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.handler = lambda request: httpx.Response(200, json=COMPLETION)
        self.sleeping = False
        self.props_calls = 0
        self.props_handler = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            self.props_calls += 1
            if self.props_handler:
                return self.props_handler(request)
            return httpx.Response(200, json={"is_sleeping": self.sleeping})
        self.requests.append(request)
        response = self.handler(request)
        if asyncio.iscoroutine(response):
            response = await response
        return response

    def last_body(self) -> dict:
        return json.loads(self.requests[-1].content)


def _settings_with(**overrides) -> Settings:
    data = get_settings().model_dump()
    data.update(
        LLM_API_URL=BASE_URL,
        LLM_API_KEY="llm-key",
        LLM_MAX_TOKENS=2048,
        LLM_QUEUE_SIZE=10,
    )
    data.update(overrides)
    return Settings(**data)


@pytest.fixture
def settings():
    return _settings_with()


@pytest.fixture
def upstream(settings, monkeypatch):
    fake = Upstream()
    transport = httpx.MockTransport(fake)
    original = LlmClient._client

    def _client(self):
        real = original(self)
        real._transport = transport
        return real

    monkeypatch.setattr(LlmClient, "_client", _client)
    monkeypatch.setattr(llmRoute, "_queue", llmRoute.LlmQueue())
    monkeypatch.setattr(llmRoute, "_warmup", llmRoute.LlmWarmup())
    monkeypatch.setattr(llmRoute, "_jobs", llmRoute.LlmJobs())
    with patch("app.infrastructure.http.llm_client.get_settings", return_value=settings), \
            patch("app.routers.llmRoute.get_settings", return_value=settings):
        yield fake


@pytest.fixture
async def async_client():
    app.dependency_overrides[get_current_user] = _user_file_editor
    app.dependency_overrides[authenticated_user_or_none] = _user_file_editor
    app.dependency_overrides[get_db] = mock_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


class TestAuth:
    def test_requires_credentials(self, client_no_auth, upstream):
        assert client_no_auth.post("/llm/v1/chat/completions", json=CHAT).status_code == 401
        assert client_no_auth.get("/llm/health").status_code == 401
        assert upstream.requests == []

    def test_upstream_gets_internal_key_not_client_headers(self, client, upstream):
        # Key lookup needs the database; only what reaches upstream matters here.
        app.dependency_overrides[get_current_principal] = lambda: Principal.from_user(
            make_mock_user()
        )
        response = client.post(
            "/llm/v1/chat/completions",
            json=CHAT,
            headers={"X-API-Key": "trem_client_key", "X-Custom": "x"},
        )
        assert response.status_code == 200
        sent = upstream.requests[-1].headers
        assert sent["authorization"] == "Bearer llm-key"
        assert "x-api-key" not in sent
        assert "x-custom" not in sent


class TestSanitize:
    def test_returns_upstream_json_as_is(self, client, upstream):
        response = client.post("/llm/v1/chat/completions", json=CHAT)
        assert response.status_code == 200
        assert response.json() == COMPLETION

    def test_forces_stream_off_and_drops_model(self, client, upstream):
        client.post("/llm/v1/chat/completions", json={**CHAT, "stream": True, "model": "gpt-4"})
        sent = upstream.last_body()
        assert sent["stream"] is False
        assert "model" not in sent
        assert sent["messages"] == CHAT["messages"]

    @pytest.mark.parametrize(
        "extra, expected",
        [
            ({}, 2048),
            ({"max_tokens": 100000}, 2048),
            ({"max_tokens": 64}, 64),
            ({"max_tokens": -1}, 2048),
            ({"max_completion_tokens": 128}, 128),
            ({"max_completion_tokens": 100000}, 2048),
        ],
    )
    def test_caps_max_tokens(self, client, upstream, extra, expected):
        client.post("/llm/v1/chat/completions", json={**CHAT, **extra})
        sent = upstream.last_body()
        assert sent["max_tokens"] == expected
        assert "max_completion_tokens" not in sent


class TestErrors:
    @pytest.mark.parametrize("upstream_status, expected", [(400, 400), (401, 502), (500, 502)])
    def test_upstream_status(self, client, upstream, upstream_status, expected):
        upstream.handler = lambda r: httpx.Response(
            upstream_status, json={"error": {"message": "bad field"}}
        )
        response = client.post("/llm/v1/chat/completions", json=CHAT)
        assert response.status_code == expected
        if expected == 400:
            assert response.json()["detail"] == "bad field"

    @pytest.mark.parametrize(
        "error, expected",
        [(httpx.ConnectError("refused"), 503), (httpx.ReadTimeout("slow"), 504)],
    )
    def test_transport_errors(self, client, upstream, error, expected):
        def handler(request):
            raise error

        upstream.handler = handler
        assert client.post("/llm/v1/chat/completions", json=CHAT).status_code == expected

    def test_unconfigured_url(self, client, upstream):
        with patch(
            "app.infrastructure.http.llm_client.get_settings",
            return_value=_settings_with(LLM_API_URL=None),
        ):
            assert client.post("/llm/v1/chat/completions", json=CHAT).status_code == 503
            assert client.get("/llm/health").status_code == 503

    def test_body_over_limit_is_413(self, client, upstream):
        big = {"messages": [{"role": "user", "content": "x" * 70000}]}
        response = client.post("/llm/v1/chat/completions", json=big)
        assert response.status_code == 413
        assert upstream.requests == []


class TestQueue:
    async def test_second_call_waits_for_the_first(self, async_client, upstream):
        release = asyncio.Event()
        in_flight = 0
        peak = 0

        async def slow(request):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await release.wait()
            in_flight -= 1
            return httpx.Response(200, json=COMPLETION)

        upstream.handler = slow
        first = asyncio.create_task(async_client.post("/llm/v1/chat/completions", json=CHAT))
        second = asyncio.create_task(async_client.post("/llm/v1/chat/completions", json=CHAT))
        await asyncio.sleep(0.2)
        assert len(upstream.requests) == 1
        release.set()
        responses = await asyncio.gather(first, second)

        assert [r.status_code for r in responses] == [200, 200]
        assert len(upstream.requests) == 2
        assert peak == 1

    async def test_full_queue_answers_503(self, async_client, upstream, settings):
        settings.LLM_QUEUE_SIZE = 1
        release = asyncio.Event()

        async def slow(request):
            await release.wait()
            return httpx.Response(200, json=COMPLETION)

        upstream.handler = slow
        first = asyncio.create_task(async_client.post("/llm/v1/chat/completions", json=CHAT))
        await asyncio.sleep(0.2)
        rejected = await async_client.post("/llm/v1/chat/completions", json=CHAT)
        release.set()

        assert rejected.status_code == 503
        assert rejected.headers["retry-after"] == "60"
        assert (await first).status_code == 200
        assert len(upstream.requests) == 1


class TestLogs:
    def test_logs_metrics_never_content(self, client, upstream, caplog):
        upstream.handler = lambda r: httpx.Response(
            200,
            json={
                **COMPLETION,
                "choices": [
                    {"message": {"content": SENTINEL, "reasoning_content": SENTINEL}}
                ],
            },
        )
        caplog.set_level(logging.DEBUG)
        payload = {"messages": [{"role": "user", "content": SENTINEL}]}
        assert client.post("/llm/v1/chat/completions", json=payload).status_code == 200

        upstream.handler = lambda r: httpx.Response(500, text=SENTINEL)
        assert client.post("/llm/v1/chat/completions", json=payload).status_code == 502

        assert SENTINEL not in caplog.text
        lines = [r.getMessage() for r in caplog.records if r.name == "app.routers.llmRoute"]
        assert len(lines) == 2
        assert "status=200" in lines[0]
        assert "prompt_tokens=12 cached_tokens=8 completion_tokens=3" in lines[0]
        assert "status=502" in lines[1]


class TestHealth:
    def test_ok(self, client, upstream):
        response = client.get("/llm/health")
        assert response.status_code == 200
        assert upstream.requests[-1].url.path == "/health"

    def test_down(self, client, upstream):
        def handler(request):
            raise httpx.ConnectError("refused")

        upstream.handler = handler
        assert client.get("/llm/health").status_code == 503

    def test_reports_sleeping_without_waking(self, client, upstream):
        upstream.sleeping = True
        assert client.get("/llm/health").json() == {"status": "sleeping"}
        assert [r.url.path for r in upstream.requests] == ["/health"]
        assert not llmRoute._warmup.in_progress

    def test_reports_warming_up(self, client, upstream):
        llmRoute._warmup._since = time.monotonic()
        assert client.get("/llm/health").json() == {"status": "warming_up"}
        assert upstream.props_calls == 0


class TestWarmup:
    async def test_awake_model_follows_normal_flow(self, async_client, upstream):
        response = await async_client.post("/llm/v1/chat/completions", json=CHAT)
        assert response.status_code == 200
        assert upstream.props_calls == 1

    async def test_sleeping_model_answers_warming_up_and_wakes_once(
        self, async_client, upstream
    ):
        upstream.sleeping = True
        release = asyncio.Event()

        async def wake(request):
            await release.wait()
            return httpx.Response(200, json=COMPLETION)

        upstream.handler = wake
        first = await async_client.post("/llm/v1/chat/completions", json=CHAT)
        await asyncio.sleep(0.05)

        assert first.status_code == 503
        assert first.json()["status"] == "warming_up"
        assert first.headers["retry-after"] == str(first.json()["eta_seconds"])
        assert len(upstream.requests) == 1
        assert upstream.last_body()["max_tokens"] == 1

        llmRoute._warmup._since -= 100
        second = await async_client.post("/llm/v1/chat/completions", json=CHAT)
        assert second.status_code == 503
        assert second.json()["eta_seconds"] < first.json()["eta_seconds"]
        assert len(upstream.requests) == 1
        assert upstream.props_calls == 1

        release.set()
        await llmRoute._warmup._task
        assert not llmRoute._warmup.in_progress

        upstream.sleeping = False
        upstream.handler = lambda r: httpx.Response(200, json=COMPLETION)
        third = await async_client.post("/llm/v1/chat/completions", json=CHAT)
        assert third.status_code == 200
        assert upstream.props_calls == 2

    async def test_failed_wake_clears_state(self, async_client, upstream, caplog):
        upstream.sleeping = True
        upstream.handler = lambda r: httpx.Response(500, text=SENTINEL)
        caplog.set_level(logging.DEBUG)

        response = await async_client.post("/llm/v1/chat/completions", json=CHAT)
        assert response.status_code == 503
        await llmRoute._warmup._task

        assert not llmRoute._warmup.in_progress
        assert "llm warmup failed status=502" in caplog.text
        assert SENTINEL not in caplog.text

    @pytest.mark.parametrize(
        "props, expected",
        [
            (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), 503),
            (lambda r: httpx.Response(500), 502),
        ],
    )
    def test_props_errors(self, client, upstream, props, expected):
        upstream.props_handler = props
        assert client.post("/llm/v1/chat/completions", json=CHAT).status_code == expected
        assert upstream.requests == []

    def test_logs_warming_up_without_content(self, client, upstream, caplog):
        upstream.sleeping = True
        caplog.set_level(logging.DEBUG)
        payload = {"messages": [{"role": "user", "content": SENTINEL}]}
        assert client.post("/llm/v1/chat/completions", json=payload).status_code == 503

        lines = [r.getMessage() for r in caplog.records if r.name == "app.routers.llmRoute"]
        assert any("llm request" in line and "status=503" in line for line in lines)
        assert SENTINEL not in caplog.text


class TestWarmupEstimate:
    def test_just_started(self):
        warmup = llmRoute.LlmWarmup()
        warmup._since = time.monotonic()
        body = json.loads(warmup.response(210).body)
        assert body["eta_seconds"] in (209, 210)
        assert body["progress"] == 0.0

    def test_overdue(self):
        warmup = llmRoute.LlmWarmup()
        warmup._since = time.monotonic() - 1000
        response = warmup.response(210)
        body = json.loads(response.body)
        assert body["eta_seconds"] == 15
        assert response.headers["retry-after"] == "15"
        assert body["progress"] == 0.95


async def _finish_jobs():
    await asyncio.gather(*llmRoute._jobs._tasks)


class TestJobs:
    def test_requires_credentials(self, client_no_auth, upstream):
        assert client_no_auth.post("/llm/jobs", json=CHAT).status_code == 401
        assert client_no_auth.get("/llm/jobs/abc").status_code == 401

    async def test_submit_then_poll_result(self, async_client, upstream):
        release = asyncio.Event()

        async def slow(request):
            await release.wait()
            return httpx.Response(200, json=COMPLETION)

        upstream.handler = slow
        submitted = await async_client.post("/llm/jobs", json={**CHAT, "stream": True})
        assert submitted.status_code == 202
        job_id = submitted.json()["job_id"]
        assert submitted.json()["status"] == "queued"

        await asyncio.sleep(0.05)
        running = await async_client.get(f"/llm/jobs/{job_id}")
        assert running.json() == {"job_id": job_id, "status": "running"}

        release.set()
        await _finish_jobs()
        done = await async_client.get(f"/llm/jobs/{job_id}")
        assert done.json() == {"job_id": job_id, "status": "done", "result": COMPLETION}
        assert upstream.last_body()["stream"] is False
        assert upstream.last_body()["max_tokens"] == 2048

    async def test_upstream_error_fails_job(self, async_client, upstream):
        upstream.handler = lambda r: httpx.Response(
            400, json={"error": {"message": "context too long"}}
        )
        job_id = (await async_client.post("/llm/jobs", json=CHAT)).json()["job_id"]
        await _finish_jobs()
        body = (await async_client.get(f"/llm/jobs/{job_id}")).json()
        assert body == {"job_id": job_id, "status": "failed", "error": "context too long"}

    async def test_unexpected_error_fails_job_without_content(
        self, async_client, upstream, caplog
    ):
        upstream.handler = lambda r: httpx.Response(200, text=SENTINEL)  # not JSON
        caplog.set_level(logging.DEBUG)
        job_id = (await async_client.post("/llm/jobs", json=CHAT)).json()["job_id"]
        await _finish_jobs()
        body = (await async_client.get(f"/llm/jobs/{job_id}")).json()
        assert body["status"] == "failed"
        assert body["error"] == "LLM job failed"
        assert SENTINEL not in caplog.text

    async def test_sleeping_model_is_woken_inside_the_job(self, async_client, upstream):
        upstream.sleeping = True
        release = asyncio.Event()

        async def wake_then_answer(request):
            if request.url.path == "/health":
                return httpx.Response(200)
            if json.loads(request.content)["max_tokens"] == 1:
                await release.wait()
            return httpx.Response(200, json=COMPLETION)

        upstream.handler = wake_then_answer
        submitted = await async_client.post("/llm/jobs", json=CHAT)
        assert submitted.status_code == 202
        await asyncio.sleep(0.05)
        assert llmRoute._warmup.in_progress
        assert (await async_client.get("/llm/health")).json() == {"status": "warming_up"}

        release.set()
        await _finish_jobs()
        job_id = submitted.json()["job_id"]
        assert (await async_client.get(f"/llm/jobs/{job_id}")).json()["status"] == "done"
        chats = [r for r in upstream.requests if r.url.path == "/v1/chat/completions"]
        assert [json.loads(r.content)["max_tokens"] for r in chats] == [1, 2048]

    async def test_jobs_and_sync_calls_share_the_queue(self, async_client, upstream):
        release = asyncio.Event()
        in_flight = 0
        peak = 0

        async def slow(request):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await release.wait()
            in_flight -= 1
            return httpx.Response(200, json=COMPLETION)

        upstream.handler = slow
        await async_client.post("/llm/jobs", json=CHAT)
        sync = asyncio.create_task(async_client.post("/llm/v1/chat/completions", json=CHAT))
        await asyncio.sleep(0.1)
        assert len(upstream.requests) == 1
        release.set()
        assert (await sync).status_code == 200
        await _finish_jobs()
        assert peak == 1

    async def test_full_queue_rejects_submit(self, async_client, upstream, settings):
        settings.LLM_QUEUE_SIZE = 1
        release = asyncio.Event()

        async def slow(request):
            await release.wait()
            return httpx.Response(200, json=COMPLETION)

        upstream.handler = slow
        assert (await async_client.post("/llm/jobs", json=CHAT)).status_code == 202
        rejected = await async_client.post("/llm/jobs", json=CHAT)
        assert rejected.status_code == 503
        assert rejected.headers["retry-after"] == "60"
        release.set()
        await _finish_jobs()
        assert llmRoute._queue._waiting == 0

    async def test_other_principal_cannot_read(self, async_client, upstream):
        job_id = (await async_client.post("/llm/jobs", json=CHAT)).json()["job_id"]
        await _finish_jobs()
        llmRoute._jobs._jobs[job_id].owner_id = "someone-else"
        assert (await async_client.get(f"/llm/jobs/{job_id}")).status_code == 404

    def test_unknown_job_is_404(self, client, upstream):
        assert client.get("/llm/jobs/nope").status_code == 404

    def test_unconfigured_url(self, client, upstream):
        with patch(
            "app.infrastructure.http.llm_client.get_settings",
            return_value=_settings_with(LLM_API_URL=None),
        ):
            assert client.post("/llm/jobs", json=CHAT).status_code == 503
        assert llmRoute._jobs._jobs == {}

    def test_body_over_limit_is_413(self, client, upstream):
        big = {"messages": [{"role": "user", "content": "x" * 70000}]}
        assert client.post("/llm/jobs", json=big).status_code == 413
        assert llmRoute._jobs._jobs == {}

    async def test_finished_jobs_expire(self, async_client, upstream):
        job_id = (await async_client.post("/llm/jobs", json=CHAT)).json()["job_id"]
        await _finish_jobs()
        llmRoute._jobs._jobs[job_id].finished_at -= 3601
        assert (await async_client.get(f"/llm/jobs/{job_id}")).status_code == 404

    async def test_logs_job_metrics_never_content(self, async_client, upstream, caplog):
        caplog.set_level(logging.DEBUG)
        payload = {"messages": [{"role": "user", "content": SENTINEL}]}
        await async_client.post("/llm/jobs", json=payload)
        await _finish_jobs()
        lines = [r.getMessage() for r in caplog.records if r.name == "app.routers.llmRoute"]
        assert any(line.startswith("llm job ") and "status=200" in line for line in lines)
        assert SENTINEL not in caplog.text
