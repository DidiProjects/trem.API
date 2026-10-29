"""
Remote LinkedIn login through trem-api: one-time token to cookie, the viewer files and the
WebSocket relay, with the orchestrator replaced by httpx.MockTransport and a websockets server.
"""
import asyncio
import threading
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve

from app.api.v1.dependencies import get_current_principal
from app.core.config import Settings, get_settings
from app.domain.entities.api_client import Principal, PrincipalKind
from app.infrastructure.database.connection import get_db
from app.infrastructure.http.scraper_client import ScraperClient
from app.main import app
from app.services import scraperLoginService
from app.services.scraperLoginService import LoginGrants, get_login_grants
from tests.conftest import mock_get_db

INTERNAL_KEY = "internal-key-" + "x" * 32
OWNER = "00000000-0000-0000-0000-0000000000aa"
LOGIN = "b" * 32
OTHER_LOGIN = "c" * 32
PREFIX = "/scrapers/linkedin"


class TestLoginGrants:
    def test_token_is_single_use_and_bound_to_its_login(self):
        grants = LoginGrants()
        token = grants.issue(LOGIN, OWNER)
        assert grants.redeem(token, OTHER_LOGIN) is None
        token = grants.issue(LOGIN, OWNER)
        cookie = grants.redeem(token, LOGIN)
        assert cookie
        assert grants.redeem(token, LOGIN) is None
        assert grants.owner_for(cookie, LOGIN) == OWNER
        assert grants.owner_for(cookie, OTHER_LOGIN) is None
        assert grants.owner_for("forged", LOGIN) is None
        assert grants.owner_for("", LOGIN) is None

    def test_only_digests_are_kept(self):
        grants = LoginGrants()
        token = grants.issue(LOGIN, OWNER)
        cookie = grants.redeem(grants.issue(LOGIN, OWNER), LOGIN)
        kept = list(grants._tokens) + list(grants._cookies)
        assert token not in kept and cookie not in kept

    def test_token_and_cookie_expire(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(scraperLoginService.time, "monotonic", lambda: clock[0])
        grants = LoginGrants()
        late = grants.issue(LOGIN, OWNER)
        clock[0] += scraperLoginService.TOKEN_TTL_SECONDS + 1
        assert grants.redeem(late, LOGIN) is None
        cookie = grants.redeem(grants.issue(LOGIN, OWNER), LOGIN)
        clock[0] += scraperLoginService.VIEWER_TTL_SECONDS + 1
        assert grants.owner_for(cookie, LOGIN) is None


class Orchestrator:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/session/login" and request.method == "POST":
            return httpx.Response(201, json={"id": LOGIN, "status": "starting", "error": None, "expires_at": "x"})
        if path.endswith("/files/vnc.html"):
            return httpx.Response(200, text="<html>novnc</html>", headers={"content-type": "text/html"})
        if path.endswith("/files/app/ui.js"):
            return httpx.Response(200, text="export {}", headers={"content-type": "application/javascript"})
        if path == f"/session/login/{LOGIN}":
            return httpx.Response(200, json={"id": LOGIN, "status": "waiting"})
        return httpx.Response(404, json={"detail": "Not found"})


class FakeStream:
    """The orchestrator's WebSocket: records the handshake headers and echoes frames."""

    def __init__(self):
        self.handshakes = []
        self.ready = threading.Event()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._serve())

    async def _serve(self):
        async def echo(ws):
            self.handshakes.append((ws.request.path, dict(ws.request.headers), ws.subprotocol))
            async for message in ws:
                await ws.send(b"echo:" + message)

        async with serve(echo, "127.0.0.1", 0, subprotocols=["binary"]) as server:
            self.port = server.sockets[0].getsockname()[1]
            self.ready.set()
            await asyncio.Future()


@pytest.fixture(scope="module")
def stream():
    return FakeStream()


@pytest.fixture
def grants():
    fresh = LoginGrants()
    app.dependency_overrides[get_login_grants] = lambda: fresh
    yield fresh


@pytest.fixture
def orchestrator(monkeypatch, stream, grants):
    fake = Orchestrator()
    transport = httpx.MockTransport(fake)
    original = ScraperClient._client

    def _client(self):
        real = original(self)
        real._transport = transport
        return real

    monkeypatch.setattr(ScraperClient, "_client", _client)
    data = get_settings().model_dump() | {
        "SCRAPER_API_URL": f"http://127.0.0.1:{stream.port}", "SCRAPER_API_KEY": INTERNAL_KEY,
    }
    with patch("app.infrastructure.http.scraper_client.get_settings", return_value=Settings(**data)):
        yield fake
    app.dependency_overrides.clear()


def _principal(profiles=("linkedin_scraper",), user_id=OWNER):
    return Principal(id="k", display_name="k", profile_names=frozenset(profiles),
                     kind=PrincipalKind.CLIENT, user_id=user_id)


@pytest.fixture
def api(orchestrator):
    app.dependency_overrides[get_current_principal] = lambda: _principal()
    app.dependency_overrides[get_db] = mock_get_db
    return TestClient(app)


@pytest.fixture
def browser(orchestrator):
    """A plain browser: no API key, follows nothing by itself."""
    app.dependency_overrides[get_db] = mock_get_db
    return TestClient(app, follow_redirects=False)


def _start(api) -> str:
    response = api.post(f"{PREFIX}/session/login")
    assert response.status_code == 201
    return response.json()["url"]


def _open(browser, url) -> httpx.Response:
    response = browser.get(url)
    assert response.status_code == 303
    return response


class TestStartLogin:
    def test_returns_a_viewer_url_with_a_one_time_token(self, api, orchestrator):
        body = api.post(f"{PREFIX}/session/login").json()
        assert body["id"] == LOGIN and body["status"] == "starting"
        assert body["url"].startswith(f"{PREFIX}/login/{LOGIN}/start?token=")
        sent = orchestrator.requests[-1]
        assert (sent.method, sent.url.path, sent.headers["x-owner-id"]) == ("POST", "/session/login", OWNER)

    def test_requires_the_scraper_profile_and_an_owner(self, orchestrator):
        app.dependency_overrides[get_db] = mock_get_db
        for principal in (_principal(("ai",)), _principal(user_id=None)):
            app.dependency_overrides[get_current_principal] = lambda p=principal: p
            assert TestClient(app).post(f"{PREFIX}/session/login").status_code == 403
        assert orchestrator.requests == []

    def test_state_and_cancel_are_forwarded(self, api, orchestrator):
        assert api.get(f"{PREFIX}/session/login/{LOGIN}").json()["status"] == "waiting"
        api.delete(f"{PREFIX}/session/login/{LOGIN}")
        assert (orchestrator.requests[-1].method, orchestrator.requests[-1].url.path) == ("DELETE", f"/session/login/{LOGIN}")
        assert api.get(f"{PREFIX}/session/login/not-an-id").status_code == 422


class TestViewer:
    def test_token_becomes_a_cookie_scoped_to_the_login(self, api, browser):
        response = _open(browser, _start(api))
        location = response.headers["location"]
        assert location.startswith(f"{PREFIX}/login/{LOGIN}/files/vnc.html?")
        assert f"path=scrapers/linkedin/login/{LOGIN}/websockify" in location
        cookie = response.headers["set-cookie"].lower()
        assert "scr_login=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        assert f"path={PREFIX}/login/{LOGIN}/" in cookie
        assert response.headers["referrer-policy"] == "no-referrer"

    def test_token_works_once(self, api, browser):
        url = _start(api)
        _open(browser, url)
        assert browser.get(url).status_code == 403

    def test_token_of_one_login_does_not_open_another(self, api, browser):
        url = _start(api).replace(LOGIN, OTHER_LOGIN, 1)
        assert browser.get(url).status_code == 403

    def test_files_need_the_cookie(self, api, browser, orchestrator):
        assert browser.get(f"{PREFIX}/login/{LOGIN}/files/vnc.html").status_code == 403
        _open(browser, _start(api))
        response = browser.get(f"{PREFIX}/login/{LOGIN}/files/vnc.html")
        assert (response.status_code, response.text) == (200, "<html>novnc</html>")
        assert response.headers["x-frame-options"] == "DENY"
        assert browser.get(f"{PREFIX}/login/{LOGIN}/files/app/ui.js").headers["content-type"].startswith("application/javascript")
        sent = orchestrator.requests[-1]
        assert sent.headers["x-owner-id"] == OWNER and sent.headers["x-internal-key"] == INTERNAL_KEY

    def test_cookie_of_one_login_does_not_open_another(self, api, browser):
        _open(browser, _start(api))
        cookie = browser.cookies.get("scr_login")
        other = TestClient(app, follow_redirects=False, cookies={"scr_login": cookie})
        assert other.get(f"{PREFIX}/login/{OTHER_LOGIN}/files/vnc.html").status_code == 403

    def test_file_paths_are_restricted(self, api, browser, orchestrator):
        _open(browser, _start(api))
        before = len(orchestrator.requests)
        for path in ("..%2F..%2Fsession", "app/../../x", "a b.js"):
            assert browser.get(f"{PREFIX}/login/{LOGIN}/files/{path}").status_code == 404
        assert len(orchestrator.requests) == before
        assert browser.get(f"{PREFIX}/login/{LOGIN}/files/missing.js").status_code == 404


class TestStream:
    def _viewer(self, api, browser):
        _open(browser, _start(api))
        return browser

    def test_relays_with_the_server_credentials(self, api, browser, stream):
        viewer = self._viewer(api, browser)
        with viewer.websocket_connect(
            f"{PREFIX}/login/{LOGIN}/websockify", subprotocols=["binary"], headers={"origin": "http://testserver"}
        ) as ws:
            assert ws.accepted_subprotocol == "binary"
            ws.send_bytes(b"\x01key")
            assert ws.receive_bytes() == b"echo:\x01key"
        path, headers, subprotocol = stream.handshakes[-1]
        assert path == f"/session/login/{LOGIN}/websockify"
        assert headers["x-owner-id"] == OWNER and headers["x-internal-key"] == INTERNAL_KEY
        assert subprotocol == "binary"
        assert "cookie" not in {k.lower() for k in headers}

    @pytest.mark.parametrize("origin", [None, "https://evil.example", "http://testserver.evil.example"])
    def test_other_origins_are_refused(self, api, browser, origin):
        viewer = self._viewer(api, browser)
        headers = {"origin": origin} if origin else {}
        with pytest.raises(WebSocketDisconnect):
            with viewer.websocket_connect(f"{PREFIX}/login/{LOGIN}/websockify", headers=headers) as ws:
                ws.receive_bytes()

    def test_without_cookie_is_refused(self, orchestrator, stream):
        before = len(stream.handshakes)
        with pytest.raises(WebSocketDisconnect):
            with TestClient(app).websocket_connect(
                f"{PREFIX}/login/{LOGIN}/websockify", headers={"origin": "http://testserver"}
            ) as ws:
                ws.receive_bytes()
        assert len(stream.handshakes) == before
