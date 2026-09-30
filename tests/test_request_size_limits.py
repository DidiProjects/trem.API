import asyncio
import io
import json
import typing

import pytest
from fastapi import FastAPI, File, UploadFile
from fastapi.testclient import TestClient

from app.api.middleware.body_size_limit import BodySizeLimitMiddleware
from app.core.config import get_settings
from app.main import app

MB = 1024 * 1024

# Well above nginx's 1 MB default, which is what broke production uploads.
PROXY_DEFAULT_OVERFLOW = 5 * MB


def _is_binary(prop: dict) -> bool:
    prop = prop.get("items", prop)
    return prop.get("format") == "binary" or "contentMediaType" in prop


def _discover_upload_fields() -> dict[str, str]:
    # Read from OpenAPI: included routers are resolved lazily, so app.routes
    # does not list the endpoints themselves.
    schema = app.openapi()
    fields = {}
    for path, operations in schema["paths"].items():
        content = operations.get("post", {}).get("requestBody", {}).get("content", {})
        ref = content.get("multipart/form-data", {}).get("schema", {}).get("$ref")
        if not ref:
            continue
        properties = schema["components"]["schemas"][ref.split("/")[-1]]["properties"]
        field = next((name for name, prop in properties.items() if _is_binary(prop)), None)
        if field:
            fields[path] = field
    return fields


UPLOAD_FIELDS = _discover_upload_fields()
UPLOAD_PATHS = sorted(UPLOAD_FIELDS)


def _limit_for(path: str) -> int:
    middleware = next(m for m in app.user_middleware if m.cls is BodySizeLimitMiddleware)
    return BodySizeLimitMiddleware(None, **middleware.kwargs).limit_for(path)


def test_discovers_every_upload_endpoint():
    # Guards the parametrization below: a broken discovery would test nothing.
    assert len(UPLOAD_PATHS) >= 17
    assert {"/image/to-pdf", "/pdf/merge", "/audio/transcribe", "/movie/cut"} <= set(UPLOAD_PATHS)


@pytest.mark.parametrize("path", UPLOAD_PATHS)
def test_every_upload_endpoint_allows_more_than_proxy_default(path):
    assert _limit_for(path) >= get_settings().MAX_REQUEST_SIZE


@pytest.mark.parametrize("path", UPLOAD_PATHS)
def test_upload_above_proxy_default_is_not_rejected_for_size(client, path):
    payload = b"\0" * PROXY_DEFAULT_OVERFLOW
    response = client.post(
        path,
        files=[(UPLOAD_FIELDS[path], ("upload.bin", io.BytesIO(payload), "application/octet-stream"))],
    )
    assert response.status_code != 413


async def _call_asgi(path: str, headers: list[tuple[bytes, bytes]], chunks: list[bytes]):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }
    pending = list(chunks)
    sent = []

    async def receive():
        if pending:
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1, "the app must not answer after the 413"
    start = starts[0]
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


@pytest.mark.parametrize("path", UPLOAD_PATHS)
def test_declared_oversized_body_is_rejected_before_reading(path):
    limit = _limit_for(path)
    status, headers, body = asyncio.run(_call_asgi(
        path,
        [
            (b"content-type", b"multipart/form-data; boundary=x"),
            (b"content-length", str(limit + 1).encode()),
        ],
        [b""],
    ))
    assert status == 413
    assert headers[b"content-type"] == b"application/json"
    assert "MB limit" in json.loads(body)["detail"]


@pytest.mark.parametrize("path", UPLOAD_PATHS)
def test_chunked_oversized_body_is_rejected_while_streaming(path):
    limit = _limit_for(path)
    head = (
        b"--x\r\n"
        + f'Content-Disposition: form-data; name="{UPLOAD_FIELDS[path]}"; filename="big.bin"\r\n'.encode()
        + b"Content-Type: application/octet-stream\r\n\r\n"
    )
    chunks = [head] + [b"\0" * MB] * (limit // MB + 2)
    status, headers, body = asyncio.run(_call_asgi(
        path,
        [(b"content-type", b"multipart/form-data; boundary=x"), (b"transfer-encoding", b"chunked")],
        chunks,
    ))
    assert status == 413
    assert headers[b"content-type"] == b"application/json"
    assert "MB limit" in json.loads(body)["detail"]


class TestBodySizeLimitMiddleware:
    @pytest.fixture
    def limited_client(self):
        mini = FastAPI()
        mini.add_middleware(
            BodySizeLimitMiddleware,
            default_limit=1 * MB,
            limits_by_prefix={"/media": 3 * MB},
        )

        @mini.post("/doc")
        async def doc(files: typing.List[UploadFile] = File(...)):
            return {"count": len(files)}

        @mini.post("/media/cut")
        async def media(file: UploadFile = File(...)):
            return {"size": len(await file.read())}

        return TestClient(mini)

    def test_limit_applies_to_the_sum_of_files(self, limited_client):
        half = b"\0" * (MB // 2 + 1024)
        response = limited_client.post(
            "/doc",
            files=[
                ("files", ("a.bin", io.BytesIO(half), "application/octet-stream")),
                ("files", ("b.bin", io.BytesIO(half), "application/octet-stream")),
            ],
        )
        assert response.status_code == 413

    def test_prefix_limit_overrides_default(self, limited_client):
        payload = b"\0" * (2 * MB)
        response = limited_client.post(
            "/media/cut",
            files={"file": ("a.bin", io.BytesIO(payload), "application/octet-stream")},
        )
        assert response.status_code == 200
        assert response.json() == {"size": 2 * MB}

    def test_prefix_does_not_match_sibling_paths(self):
        middleware = BodySizeLimitMiddleware(None, default_limit=1, limits_by_prefix={"/media": 2})
        assert middleware.limit_for("/media") == 2
        assert middleware.limit_for("/media/cut") == 2
        assert middleware.limit_for("/mediafoo") == 1

    def test_non_http_scopes_pass_through(self):
        seen = []

        async def inner(scope, receive, send):
            seen.append(scope["type"])

        middleware = BodySizeLimitMiddleware(inner, default_limit=1)
        asyncio.run(middleware({"type": "lifespan"}, None, None))
        assert seen == ["lifespan"]
