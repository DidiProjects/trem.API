import json
import logging
import re
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Body, Depends, File, HTTPException, Path, Query, Request, UploadFile, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.api.v1.dependencies import require_profile
from app.core.config import get_settings
from app.domain.entities.api_client import Principal
from app.infrastructure.http.scraper_client import ScraperApiError, ScraperClient, relay
from app.services.scraperLoginService import VIEWER_TTL_SECONDS, LoginGrants, get_login_grants

logger = logging.getLogger(__name__)

router = APIRouter()

linkedin_scraper = require_profile("linkedin_scraper")

# Orchestrator job and login ids are uuid4 hex. Checked here so nothing but an id reaches its URL path.
JobId = Path(pattern=r"^[0-9a-f]{32}$")
LoginId = Path(pattern=r"^[0-9a-f]{32}$")
# noVNC static files: the characters its tree uses, never "..".
_VIEWER_FILE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9_./-]{1,200}$")
VIEWER_COOKIE = "scr_login"

# What the orchestrator answers about the caller's own request is passed on as is.
_CALLER_ERRORS = {400, 404, 409, 413, 422}

MAX_CV_BYTES = 2 * 1024 * 1024


async def owner(principal: Principal = Depends(linkedin_scraper)) -> str:
    """Scraper jobs belong to a person: a service key without an owner has nobody to act for."""
    if not principal.user_id:
        raise HTTPException(
            status_code=403,
            detail="Scraper jobs belong to a user: use an API key owned by a user, or log in",
        )
    return principal.user_id


async def _forward(method: str, path: str, owner_id: str, **kwargs) -> Response:
    try:
        resp = await ScraperClient().request(method, path, owner_id, **kwargs)
    except ScraperApiError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    if resp.status_code == 204:
        return Response(status_code=204)
    if resp.is_success:
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    if resp.status_code in _CALLER_ERRORS:
        raise HTTPException(status_code=resp.status_code, detail=_detail(resp))
    # 401 means the internal key is wrong: a configuration problem, not the caller's.
    logger.error("scraper %s %s answered %d", method, path.split("/")[1], resp.status_code)
    raise HTTPException(status_code=502, detail="Scraper service error")


def _detail(resp: httpx.Response):
    try:
        return resp.json().get("detail", "Request rejected")
    except ValueError:
        return "Request rejected"


@router.get("/jobs")
async def list_jobs(owner_id: str = Depends(owner)):
    return await _forward("GET", "/jobs", owner_id)


@router.post("/jobs", status_code=201)
async def create_job(body: dict = Body(...), owner_id: str = Depends(owner)):
    """Job config: name, jobs, posts, analysis, schedule, notify_to (see the README)."""
    return await _forward("POST", "/jobs", owner_id, json=body)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str = JobId, owner_id: str = Depends(owner)):
    return await _forward("GET", f"/jobs/{job_id}", owner_id)


@router.patch("/jobs/{job_id}")
async def update_job(job_id: str = JobId, body: dict = Body(...), owner_id: str = Depends(owner)):
    """Top-level sections in the body replace the current ones whole."""
    return await _forward("PATCH", f"/jobs/{job_id}", owner_id, json=body)


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str = JobId, owner_id: str = Depends(owner)):
    return await _forward("DELETE", f"/jobs/{job_id}", owner_id)


@router.put("/jobs/{job_id}/cv", status_code=204)
async def upload_cv(job_id: str = JobId, file: UploadFile = File(...), owner_id: str = Depends(owner)):
    """The CV as a .docx file."""
    content = await file.read(MAX_CV_BYTES + 1)
    if len(content) > MAX_CV_BYTES:
        raise HTTPException(status_code=413, detail=f"CV larger than {MAX_CV_BYTES} bytes")
    return await _forward("PUT", f"/jobs/{job_id}/cv", owner_id, content=content)


@router.post("/jobs/{job_id}/pause")
async def pause_job(job_id: str = JobId, owner_id: str = Depends(owner)):
    return await _forward("POST", f"/jobs/{job_id}/pause", owner_id)


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str = JobId, owner_id: str = Depends(owner)):
    return await _forward("POST", f"/jobs/{job_id}/resume", owner_id)


@router.post("/jobs/{job_id}/run", status_code=202)
async def run_job(job_id: str = JobId, owner_id: str = Depends(owner)):
    """Queues a collection ahead of the schedule."""
    return await _forward("POST", f"/jobs/{job_id}/run", owner_id)


@router.get("/jobs/{job_id}/matches")
async def job_matches(
    job_id: str = JobId,
    min_score: int = Query(0, ge=0, le=100),
    limit: int = Query(100, ge=1, le=500),
    owner_id: str = Depends(owner),
):
    return await _forward(
        "GET", f"/jobs/{job_id}/matches", owner_id, params={"min_score": min_score, "limit": limit}
    )


@router.get("/session")
async def session_state(owner_id: str = Depends(owner)):
    """LinkedIn session of the caller: none, valid or expired."""
    return await _forward("GET", "/session", owner_id)


@router.delete("/session", status_code=204)
async def delete_session(owner_id: str = Depends(owner)):
    return await _forward("DELETE", "/session", owner_id)


# --- Remote LinkedIn login ---------------------------------------------------------------
#
# The orchestrator opens a private browser on the LinkedIn login and streams it with noVNC.
# The viewer is a page the browser navigates to, so it cannot send the API key: "Connect
# LinkedIn" returns a one-time URL whose token becomes a cookie scoped to that login's path,
# and the files and the stream accept only that cookie. The password goes from the person's
# keyboard to LinkedIn's page in that browser; nothing here reads or stores it.


def _viewer_path(login_id: str) -> str:
    return f"/scrapers/linkedin/login/{login_id}/"


@router.post("/session/login", status_code=201)
async def start_login(owner_id: str = Depends(owner), grants: LoginGrants = Depends(get_login_grants)):
    """Opens a remote browser on the LinkedIn login; open `url` in a new tab to use it."""
    response = await _forward("POST", "/session/login", owner_id)
    login = json.loads(response.body)
    token = grants.issue(login["id"], owner_id)
    return {**login, "url": f"{_viewer_path(login['id'])}start?token={token}"}


@router.get("/session/login/{login_id}")
async def login_state(login_id: str = LoginId, owner_id: str = Depends(owner)):
    """starting, waiting, connected, failed, expired or cancelled."""
    return await _forward("GET", f"/session/login/{login_id}", owner_id)


@router.delete("/session/login/{login_id}", status_code=204)
async def cancel_login(login_id: str = LoginId, owner_id: str = Depends(owner)):
    return await _forward("DELETE", f"/session/login/{login_id}", owner_id)


_VIEWER_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}


def _denied(message: str) -> HTMLResponse:
    body = f"<!doctype html><title>LinkedIn login</title><p style='font-family:sans-serif'>{message}</p>"
    return HTMLResponse(body, status_code=403, headers=_VIEWER_HEADERS)


@router.get("/login/{login_id}/start", include_in_schema=False)
async def open_viewer(
    login_id: str = LoginId, token: str = Query(""), grants: LoginGrants = Depends(get_login_grants)
):
    cookie = grants.redeem(token, login_id)
    if cookie is None:
        return _denied("This link was already used or has expired. Start the login again from the Scrapers tab.")
    ws_path = f"{_viewer_path(login_id)}websockify".lstrip("/")
    viewer = f"{_viewer_path(login_id)}files/vnc.html?path={ws_path}&autoconnect=true&resize=remote&reconnect=true"
    response = RedirectResponse(viewer, status_code=303, headers=_VIEWER_HEADERS)
    response.set_cookie(
        VIEWER_COOKIE, cookie, max_age=VIEWER_TTL_SECONDS, path=_viewer_path(login_id),
        httponly=True, samesite="strict", secure=get_settings().API_URL.startswith("https://"),
    )
    return response


@router.get("/login/{login_id}/files/{path:path}", include_in_schema=False)
async def viewer_file(
    request: Request, path: str, login_id: str = LoginId, grants: LoginGrants = Depends(get_login_grants)
):
    owner_id = grants.owner_for(request.cookies.get(VIEWER_COOKIE, ""), login_id)
    if owner_id is None:
        return _denied("This login is not open in this browser. Start it again from the Scrapers tab.")
    if not _VIEWER_FILE.fullmatch(path):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        resp = await ScraperClient().request("GET", f"/session/login/{login_id}/files/{path}", owner_id)
    except ScraperApiError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    if resp.status_code != 200:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "application/octet-stream"),
        headers=_VIEWER_HEADERS,
    )


def _same_origin(websocket: WebSocket) -> bool:
    # Cookies ride along on cross-site WebSocket handshakes in some browsers; the origin must be us.
    origin = websocket.headers.get("origin")
    return bool(origin) and urlsplit(origin).netloc == websocket.headers.get("host")


@router.websocket("/login/{login_id}/websockify")
async def viewer_stream(websocket: WebSocket, login_id: str, grants: LoginGrants = Depends(get_login_grants)):
    owner_id = None
    if re.fullmatch(r"[0-9a-f]{32}", login_id) and _same_origin(websocket):
        owner_id = grants.owner_for(websocket.cookies.get(VIEWER_COOKIE, ""), login_id)
    try:
        client = ScraperClient() if owner_id else None
    except ScraperApiError:
        client = None
    if client is None:
        await websocket.close(code=1008)
        return
    await relay(
        websocket,
        client.stream_url(f"/session/login/{login_id}/websockify"),
        client.stream_headers(owner_id),
    )
