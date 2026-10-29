import logging

import httpx
from fastapi import APIRouter, Body, Depends, File, HTTPException, Path, Query, UploadFile
from fastapi.responses import JSONResponse, Response

from app.api.v1.dependencies import require_profile
from app.domain.entities.api_client import Principal
from app.infrastructure.http.scraper_client import ScraperApiError, ScraperClient

logger = logging.getLogger(__name__)

router = APIRouter()

linkedin_scraper = require_profile("linkedin_scraper")

# Orchestrator job ids are uuid4 hex. Checked here so nothing but an id reaches its URL path.
JobId = Path(pattern=r"^[0-9a-f]{32}$")

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
