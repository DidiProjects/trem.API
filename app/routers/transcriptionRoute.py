from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from app.api.v1.dependencies import require_profile
from app.domain.entities.api_client import Principal
from app.services.transcriptionService import (
    Job,
    TranscriptionJobError,
    TranscriptionJobs,
    get_transcription_jobs,
)

# Mounted under /movie and /audio. Transcription runs as a background job:
# Cloudflare drops requests over 100 MB and responses slower than 100 s, so
# large files come in parts and the result is polled.
router = APIRouter()

file_editor = require_profile("file_editor")


class UploadRequest(BaseModel):
    filename: str
    size: int
    language: Optional[str] = None


def _job_body(job: Job) -> dict:
    body = {"job_id": job.id, "status": job.status.value}
    if job.result is not None:
        body["result"] = job.result
    if job.error is not None:
        body["error"] = job.error
    return body


def _http_error(e: TranscriptionJobError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=e.message)


@router.post("/transcribe", status_code=202)
async def transcribe_file(
    file: UploadFile = File(...),
    language: str = Form(None),
    principal: Principal = Depends(file_editor),
    jobs: TranscriptionJobs = Depends(get_transcription_jobs),
):
    """
    Uploads the whole file in one request and starts the transcription.
    Above 100 MB, use the upload in parts.
    """
    try:
        return _job_body(jobs.submit_file(principal.id, file.filename, language, file.file))
    except TranscriptionJobError as e:
        raise _http_error(e)


@router.post("/transcribe/uploads", status_code=201)
async def create_upload(
    body: UploadRequest,
    principal: Principal = Depends(file_editor),
    jobs: TranscriptionJobs = Depends(get_transcription_jobs),
):
    """Opens an upload in parts; each part goes in PUT .../parts/{index}."""
    try:
        upload = jobs.create_upload(principal.id, body.filename, body.size, body.language)
    except TranscriptionJobError as e:
        raise _http_error(e)
    return {
        "upload_id": upload.id,
        "part_size": upload.part_size,
        "total_parts": upload.total_parts,
    }


@router.put("/transcribe/uploads/{upload_id}/parts/{index}", status_code=204)
async def upload_part(
    upload_id: str,
    index: int,
    request: Request,
    principal: Principal = Depends(file_editor),
    jobs: TranscriptionJobs = Depends(get_transcription_jobs),
):
    """Receives one part as the raw request body (application/octet-stream)."""
    try:
        await jobs.write_part(principal.id, upload_id, index, request.stream())
    except TranscriptionJobError as e:
        raise _http_error(e)
    return Response(status_code=204)


@router.post("/transcribe/uploads/{upload_id}/complete", status_code=202)
async def complete_upload(
    upload_id: str,
    principal: Principal = Depends(file_editor),
    jobs: TranscriptionJobs = Depends(get_transcription_jobs),
):
    """Closes the upload and starts the transcription."""
    try:
        return _job_body(jobs.complete_upload(principal.id, upload_id))
    except TranscriptionJobError as e:
        raise _http_error(e)


@router.delete("/transcribe/uploads/{upload_id}", status_code=204)
async def abort_upload(
    upload_id: str,
    principal: Principal = Depends(file_editor),
    jobs: TranscriptionJobs = Depends(get_transcription_jobs),
):
    """Cancels the upload and deletes the parts received."""
    try:
        jobs.abort_upload(principal.id, upload_id)
    except TranscriptionJobError as e:
        raise _http_error(e)
    return Response(status_code=204)


@router.get("/transcribe/jobs/{job_id}")
async def get_job(
    job_id: str,
    principal: Principal = Depends(file_editor),
    jobs: TranscriptionJobs = Depends(get_transcription_jobs),
):
    """Job state: queued, running, done (with `result`) or failed (with `error`)."""
    try:
        return _job_body(jobs.get_job(principal.id, job_id))
    except TranscriptionJobError as e:
        raise _http_error(e)
