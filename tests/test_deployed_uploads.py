"""
Uploads against the deployed API, through Cloudflare and the reverse proxy.

The in-process tests never cross the proxy, which is where the production
413 came from. Run after each deploy:

    SMOKE_API_URL=https://api.didilv93.com SMOKE_API_KEY=... pytest tests/test_deployed_uploads.py
"""
import os

import httpx
import pytest

API_URL = os.environ.get("SMOKE_API_URL")
API_KEY = os.environ.get("SMOKE_API_KEY")

pytestmark = pytest.mark.skipif(
    not (API_URL and API_KEY), reason="SMOKE_API_URL and SMOKE_API_KEY not set"
)

MB = 1024 * 1024

# The .bin name makes the app refuse the file right after reading it, so each
# check costs one upload and no processing. What matters is who answers.
ENDPOINTS = [
    ("/pdf/info", "file"),
    ("/pdf/merge", "files"),
    ("/image/to-pdf", "files"),
    ("/audio/cut", "file"),
    ("/movie/cut", "file"),
]


def _post(path: str, field: str, size: int) -> httpx.Response:
    return httpx.post(
        f"{API_URL.rstrip('/')}{path}",
        headers={"X-API-Key": API_KEY},
        files=[(field, ("smoke.bin", b"\0" * size, "application/octet-stream"))],
        data={"start": "0", "end": "1"},
        timeout=120,
    )


def _answered_by_app(response: httpx.Response) -> bool:
    return response.headers.get("content-type", "").startswith("application/json")


@pytest.mark.parametrize("path,field", ENDPOINTS)
def test_upload_above_proxy_default_reaches_the_app(path, field):
    response = _post(path, field, 5 * MB)
    assert response.status_code != 413, response.text[:200]
    assert _answered_by_app(response), response.text[:200]


def test_oversized_upload_is_rejected_by_the_app():
    response = _post("/pdf/info", "file", 51 * MB)
    assert response.status_code == 413
    assert _answered_by_app(response), response.text[:200]
    assert "MB limit" in response.json()["detail"]
