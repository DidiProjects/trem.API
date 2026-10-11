import asyncio
import math
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Optional

from app.core.config import get_settings
from app.services.audioService import AudioServiceError, transcribe, validate_transcription_input

_COPY_CHUNK = 1024 * 1024


class TranscriptionJobError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Upload:
    id: str
    owner_id: str
    ext: str
    language: Optional[str]
    size: int
    part_size: int
    touched_at: float
    received: set[int] = field(default_factory=set)

    @property
    def total_parts(self) -> int:
        return math.ceil(self.size / self.part_size)

    def part_length(self, index: int) -> int:
        return min(self.part_size, self.size - index * self.part_size)


@dataclass
class Job:
    id: str
    owner_id: str
    status: JobStatus = JobStatus.QUEUED
    result: Optional[dict] = None
    error: Optional[str] = None
    finished_at: Optional[float] = None

    @property
    def finished(self) -> bool:
        return self.status in (JobStatus.DONE, JobStatus.FAILED)


class TranscriptionJobs:
    """
    Uploads and jobs live in memory; their files live under `root`, one
    directory per id. Nothing on disk outlives the process that knows about it:
    the directory is removed when the job ends or the upload is aborted or
    expires, and `reset_storage` wipes `root` at startup, since after a crash
    whatever is there belongs to no one. That assumes a single API process.
    """

    def __init__(self, root: Path, part_size: int, max_size: int, ttl_seconds: int):
        self.root = Path(root)
        self.part_size = part_size
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._uploads: dict[str, Upload] = {}
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        # Whisper saturates the CPU and its model is shared: one job at a time.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcription")

    def reset_storage(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _dir(self, item_id: str) -> Path:
        return self.root / item_id

    def _input_path(self, item_id: str, ext: str) -> Path:
        return self._dir(item_id) / f"input{ext}"

    def _discard(self, item_id: str) -> None:
        shutil.rmtree(self._dir(item_id), ignore_errors=True)

    @staticmethod
    def _validate(filename: str, language: Optional[str]) -> str:
        try:
            return validate_transcription_input(filename, language)
        except AudioServiceError as e:
            raise TranscriptionJobError(e.message, e.status_code)

    # -- chunked upload ----------------------------------------------------

    def create_upload(
        self, owner_id: str, filename: str, size: int, language: Optional[str]
    ) -> Upload:
        ext = self._validate(filename, language)
        if size <= 0:
            raise TranscriptionJobError("File size must be greater than zero")
        if size > self.max_size:
            raise TranscriptionJobError(
                f"File exceeds the {self.max_size // (1024 * 1024)} MB limit", 413
            )
        self.root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.root).free < size:
            raise TranscriptionJobError("Not enough disk space for this file", 507)

        upload = Upload(
            id=uuid.uuid4().hex,
            owner_id=owner_id,
            ext=ext,
            language=language or None,
            size=size,
            part_size=self.part_size,
            touched_at=time.time(),
        )
        with self._lock:
            self._dir(upload.id).mkdir()
            # Parts land at their own offset, so they can arrive in any order
            # and a retried part simply overwrites itself.
            with open(self._input_path(upload.id, ext), "wb") as f:
                f.truncate(size)
            self._uploads[upload.id] = upload
        return upload

    def _get_upload(self, owner_id: str, upload_id: str) -> Upload:
        upload = self._uploads.get(upload_id)
        if upload is None or upload.owner_id != owner_id:
            raise TranscriptionJobError("Upload not found", 404)
        return upload

    async def write_part(
        self, owner_id: str, upload_id: str, index: int, chunks: AsyncIterator[bytes]
    ) -> None:
        upload = self._get_upload(owner_id, upload_id)
        if not 0 <= index < upload.total_parts:
            raise TranscriptionJobError(
                f"Part index must be between 0 and {upload.total_parts - 1}"
            )
        expected = upload.part_length(index)
        upload.touched_at = time.time()

        written = 0
        try:
            with open(self._input_path(upload.id, upload.ext), "r+b") as f:
                f.seek(index * upload.part_size)
                async for chunk in chunks:
                    written += len(chunk)
                    if written > expected:
                        break
                    f.write(chunk)
        except FileNotFoundError:
            raise TranscriptionJobError("Upload not found", 404)
        if written != expected:
            raise TranscriptionJobError(f"Part {index} must be exactly {expected} bytes")

        with self._lock:
            upload.received.add(index)
            upload.touched_at = time.time()

    def complete_upload(self, owner_id: str, upload_id: str) -> Job:
        with self._lock:
            upload = self._get_upload(owner_id, upload_id)
            missing = upload.total_parts - len(upload.received)
            if missing:
                raise TranscriptionJobError(f"{missing} part(s) not uploaded yet", 409)
            del self._uploads[upload_id]
            job = Job(id=upload.id, owner_id=owner_id)
            self._jobs[job.id] = job
        self._executor.submit(
            self._run, job, self._input_path(upload.id, upload.ext), upload.language
        )
        return job

    def abort_upload(self, owner_id: str, upload_id: str) -> None:
        with self._lock:
            self._get_upload(owner_id, upload_id)
            del self._uploads[upload_id]
            self._discard(upload_id)

    # -- single request ----------------------------------------------------

    def submit_file(
        self, owner_id: str, filename: str, language: Optional[str], source: BinaryIO
    ) -> Job:
        ext = self._validate(filename, language)
        job = Job(id=uuid.uuid4().hex, owner_id=owner_id)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self._dir(job.id).mkdir()
            with open(self._input_path(job.id, ext), "wb") as f:
                shutil.copyfileobj(source, f, _COPY_CHUNK)
        except BaseException:
            self._discard(job.id)
            raise
        with self._lock:
            self._jobs[job.id] = job
        self._executor.submit(self._run, job, self._input_path(job.id, ext), language or None)
        return job

    # -- jobs --------------------------------------------------------------

    def _run(self, job: Job, input_path: Path, language: Optional[str]) -> None:
        job.status = JobStatus.RUNNING
        status = JobStatus.FAILED
        try:
            job.result = transcribe(str(input_path), language, work_dir=str(input_path.parent))
            status = JobStatus.DONE
        except AudioServiceError as e:
            job.error = e.message
        except Exception as e:
            job.error = f"Transcription failed: {e}"
        finally:
            # Files go before the status flips, so a finished job never has any.
            self._discard(job.id)
            job.finished_at = time.time()
            job.status = status

    def get_job(self, owner_id: str, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None or job.owner_id != owner_id:
            raise TranscriptionJobError("Job not found", 404)
        return job

    # -- expiry ------------------------------------------------------------

    def sweep(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        with self._lock:
            for upload in [u for u in self._uploads.values() if u.touched_at < cutoff]:
                del self._uploads[upload.id]
                self._discard(upload.id)
            for job in [j for j in self._jobs.values() if j.finished and j.finished_at < cutoff]:
                del self._jobs[job.id]
            known = self._uploads.keys() | {j.id for j in self._jobs.values() if not j.finished}
            # Safety net for anything a bug left behind; the age check keeps it
            # off a directory whose owner is being registered right now.
            if self.root.exists():
                for entry in self.root.iterdir():
                    if entry.name in known:
                        continue
                    try:
                        stale = entry.stat().st_mtime < cutoff
                    except FileNotFoundError:
                        continue
                    if stale:
                        shutil.rmtree(entry, ignore_errors=True)


async def sweep_periodically(jobs: TranscriptionJobs, interval_seconds: float) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        jobs.sweep()


@lru_cache()
def get_transcription_jobs() -> TranscriptionJobs:
    settings = get_settings()
    return TranscriptionJobs(
        root=Path(settings.UPLOAD_DIR) / "transcriptions",
        part_size=settings.TRANSCRIPTION_PART_SIZE,
        max_size=settings.MAX_MEDIA_REQUEST_SIZE,
        ttl_seconds=settings.TRANSCRIPTION_TTL_SECONDS,
    )
