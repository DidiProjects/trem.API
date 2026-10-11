"""
Transcription jobs: upload in parts, background processing and cleanup.

Whisper is mocked; what is fixed here is the contract of the parts, who can see
what, and that no file is left on disk after any outcome.
"""
import asyncio
import io
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.audioService import AudioServiceError
from app.services.transcriptionService import (
    JobStatus,
    TranscriptionJobError,
    TranscriptionJobs,
)

OWNER = "00000000-0000-0000-0000-000000000001"
RESULT = {"text": "olá", "segments": [], "language": "pt", "duration": 1.0}


async def _chunks(*parts: bytes):
    for part in parts:
        yield part


def _write(jobs, upload, index, data, owner=OWNER):
    asyncio.run(jobs.write_part(owner, upload.id, index, _chunks(data)))


def _wait(jobs, job_id, owner=OWNER, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get_job(owner, job_id)
        if job.finished:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def _leftovers(jobs):
    return list(jobs.root.iterdir()) if jobs.root.exists() else []


@pytest.fixture
def jobs(tmp_path):
    jobs = TranscriptionJobs(tmp_path / "t", part_size=4, max_size=1024, ttl_seconds=60)
    yield jobs
    jobs.shutdown()


@pytest.fixture
def transcribe_mock():
    seen = {}

    def fake(path, language=None, work_dir=None):
        seen.update(content=open(path, "rb").read(), language=language, work_dir=work_dir)
        return RESULT

    with patch("app.services.transcriptionService.transcribe", side_effect=fake) as mock:
        mock.seen = seen
        yield mock


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class TestCreateUpload:
    def test_splits_size_into_parts(self, jobs):
        upload = jobs.create_upload(OWNER, "video.mp4", 10, "pt")
        assert upload.total_parts == 3
        assert [upload.part_length(i) for i in range(3)] == [4, 4, 2]

    @pytest.mark.parametrize(
        "filename,size,language,status",
        [
            ("planilha.xlsx", 10, None, 400),
            ("video.mp4", 10, "xx", 400),
            ("video.mp4", 0, None, 400),
            ("video.mp4", 1025, None, 413),
        ],
    )
    def test_rejects_invalid_input_without_touching_disk(self, jobs, filename, size, language, status):
        with pytest.raises(TranscriptionJobError) as exc:
            jobs.create_upload(OWNER, filename, size, language)
        assert exc.value.status_code == status
        assert _leftovers(jobs) == []

    def test_rejects_when_disk_cannot_hold_the_file(self, jobs):
        with patch(
            "app.services.transcriptionService.shutil.disk_usage",
            return_value=SimpleNamespace(free=5),
        ):
            with pytest.raises(TranscriptionJobError) as exc:
                jobs.create_upload(OWNER, "video.mp4", 10, None)
        assert exc.value.status_code == 507
        assert _leftovers(jobs) == []


class TestParts:
    def test_parts_in_any_order_rebuild_the_file(self, jobs, transcribe_mock):
        upload = jobs.create_upload(OWNER, "video.mp4", 10, "pt")
        _write(jobs, upload, 2, b"89")
        _write(jobs, upload, 0, b"0123")
        _write(jobs, upload, 1, b"4567")

        job = _wait(jobs, jobs.complete_upload(OWNER, upload.id).id)

        assert job.status == JobStatus.DONE
        assert job.result == RESULT
        assert transcribe_mock.seen["content"] == b"0123456789"
        assert transcribe_mock.seen["language"] == "pt"

    def test_part_may_arrive_in_several_chunks(self, jobs, transcribe_mock):
        upload = jobs.create_upload(OWNER, "audio.mp3", 4, None)
        asyncio.run(jobs.write_part(OWNER, upload.id, 0, _chunks(b"01", b"2", b"3")))
        _wait(jobs, jobs.complete_upload(OWNER, upload.id).id)
        assert transcribe_mock.seen["content"] == b"0123"

    def test_retried_part_overwrites_itself(self, jobs, transcribe_mock):
        upload = jobs.create_upload(OWNER, "audio.mp3", 4, None)
        _write(jobs, upload, 0, b"xxxx")
        _write(jobs, upload, 0, b"0123")
        _wait(jobs, jobs.complete_upload(OWNER, upload.id).id)
        assert transcribe_mock.seen["content"] == b"0123"

    @pytest.mark.parametrize("data", [b"012", b"01234"])
    def test_part_with_wrong_length_is_not_counted(self, jobs, data):
        upload = jobs.create_upload(OWNER, "video.mp4", 10, None)
        with pytest.raises(TranscriptionJobError) as exc:
            _write(jobs, upload, 0, data)
        assert exc.value.status_code == 400
        assert upload.received == set()

    @pytest.mark.parametrize("index", [-1, 3])
    def test_part_index_out_of_range(self, jobs, index):
        upload = jobs.create_upload(OWNER, "video.mp4", 10, None)
        with pytest.raises(TranscriptionJobError) as exc:
            _write(jobs, upload, index, b"0123")
        assert exc.value.status_code == 400

    def test_complete_with_missing_parts_is_refused(self, jobs):
        upload = jobs.create_upload(OWNER, "video.mp4", 10, None)
        _write(jobs, upload, 0, b"0123")
        with pytest.raises(TranscriptionJobError) as exc:
            jobs.complete_upload(OWNER, upload.id)
        assert exc.value.status_code == 409
        assert "2 part(s)" in exc.value.message


class TestOwnership:
    def test_other_principal_cannot_touch_upload_or_job(self, jobs, transcribe_mock):
        upload = jobs.create_upload(OWNER, "audio.mp3", 4, None)
        for action in (
            lambda: _write(jobs, upload, 0, b"0123", owner="intruder"),
            lambda: jobs.complete_upload("intruder", upload.id),
            lambda: jobs.abort_upload("intruder", upload.id),
        ):
            with pytest.raises(TranscriptionJobError) as exc:
                action()
            assert exc.value.status_code == 404

        _write(jobs, upload, 0, b"0123")
        job = _wait(jobs, jobs.complete_upload(OWNER, upload.id).id)
        with pytest.raises(TranscriptionJobError) as exc:
            jobs.get_job("intruder", job.id)
        assert exc.value.status_code == 404


class TestNoLeftovers:
    def test_finished_job_leaves_nothing_on_disk(self, jobs, transcribe_mock):
        upload = jobs.create_upload(OWNER, "audio.mp3", 4, None)
        _write(jobs, upload, 0, b"0123")
        _wait(jobs, jobs.complete_upload(OWNER, upload.id).id)
        assert _leftovers(jobs) == []

    def test_intermediate_audio_is_written_inside_the_job_directory(self, jobs, transcribe_mock):
        upload = jobs.create_upload(OWNER, "video.mp4", 4, None)
        _write(jobs, upload, 0, b"0123")
        _wait(jobs, jobs.complete_upload(OWNER, upload.id).id)
        assert transcribe_mock.seen["work_dir"] == str(jobs.root / upload.id)

    @pytest.mark.parametrize(
        "error,message",
        [
            (AudioServiceError("O vídeo não possui trilha de áudio"), "O vídeo não possui trilha de áudio"),
            (RuntimeError("boom"), "Transcription failed: boom"),
        ],
    )
    def test_failed_job_records_error_and_leaves_nothing(self, jobs, error, message):
        upload = jobs.create_upload(OWNER, "video.mp4", 4, None)
        _write(jobs, upload, 0, b"0123")
        with patch("app.services.transcriptionService.transcribe", side_effect=error):
            job = _wait(jobs, jobs.complete_upload(OWNER, upload.id).id)
        assert job.status == JobStatus.FAILED
        assert job.error == message
        assert _leftovers(jobs) == []

    def test_aborted_upload_leaves_nothing(self, jobs):
        upload = jobs.create_upload(OWNER, "video.mp4", 10, None)
        _write(jobs, upload, 0, b"0123")
        jobs.abort_upload(OWNER, upload.id)
        assert _leftovers(jobs) == []
        with pytest.raises(TranscriptionJobError):
            _write(jobs, upload, 1, b"4567")

    def test_single_request_job_leaves_nothing(self, jobs, transcribe_mock):
        job = jobs.submit_file(OWNER, "audio.mp3", None, io.BytesIO(b"abc"))
        assert _wait(jobs, job.id).status == JobStatus.DONE
        assert transcribe_mock.seen["content"] == b"abc"
        assert _leftovers(jobs) == []

    def test_single_request_with_invalid_input_writes_nothing(self, jobs):
        with pytest.raises(TranscriptionJobError):
            jobs.submit_file(OWNER, "planilha.xlsx", None, io.BytesIO(b"abc"))
        assert _leftovers(jobs) == []

    def test_reset_storage_wipes_what_a_crash_left_behind(self, jobs):
        (jobs.root / "dead-upload").mkdir(parents=True)
        (jobs.root / "dead-upload" / "input.mp4").write_bytes(b"x" * 10)
        jobs.reset_storage()
        assert jobs.root.exists()
        assert _leftovers(jobs) == []


class TestSweep:
    def _age(self, seconds):
        return time.time() - seconds

    def test_expires_idle_upload_and_its_files(self, jobs):
        upload = jobs.create_upload(OWNER, "video.mp4", 10, None)
        upload.touched_at = self._age(61)
        jobs.sweep()
        assert _leftovers(jobs) == []
        with pytest.raises(TranscriptionJobError):
            jobs.complete_upload(OWNER, upload.id)

    def test_keeps_active_upload(self, jobs):
        upload = jobs.create_upload(OWNER, "video.mp4", 10, None)
        jobs.sweep()
        assert _leftovers(jobs) == [jobs.root / upload.id]

    def test_expires_old_results_only(self, jobs, transcribe_mock):
        old = _wait(jobs, jobs.submit_file(OWNER, "a.mp3", None, io.BytesIO(b"a")).id)
        recent = _wait(jobs, jobs.submit_file(OWNER, "b.mp3", None, io.BytesIO(b"b")).id)
        old.finished_at = self._age(61)
        jobs.sweep()
        with pytest.raises(TranscriptionJobError):
            jobs.get_job(OWNER, old.id)
        assert jobs.get_job(OWNER, recent.id).status == JobStatus.DONE

    def test_removes_stale_orphan_directories(self, jobs):
        jobs.root.mkdir(parents=True)
        stale = jobs.root / "stale"
        fresh = jobs.root / "fresh"
        stale.mkdir()
        fresh.mkdir()
        os.utime(stale, (self._age(61), self._age(61)))
        jobs.sweep()
        assert _leftovers(jobs) == [fresh]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _poll(client, prefix, job_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"{prefix}/transcribe/jobs/{job_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError("job did not finish")


@pytest.mark.parametrize("prefix", ["/movie", "/audio"])
class TestRoutes:
    def test_upload_in_parts_then_poll_result(self, client, transcription_jobs, transcribe_mock, prefix):
        created = client.post(
            f"{prefix}/transcribe/uploads",
            json={"filename": "video.mp4", "size": 6, "language": "pt"},
        )
        assert created.status_code == 201
        upload = created.json()
        assert upload["part_size"] == 4
        assert upload["total_parts"] == 2

        base = f"{prefix}/transcribe/uploads/{upload['upload_id']}"
        for index, data in enumerate([b"0123", b"45"]):
            response = client.put(
                f"{base}/parts/{index}",
                content=data,
                headers={"Content-Type": "application/octet-stream"},
            )
            assert response.status_code == 204

        completed = client.post(f"{base}/complete")
        assert completed.status_code == 202
        assert completed.json()["status"] in ("queued", "running", "done")

        body = _poll(client, prefix, completed.json()["job_id"])
        assert body == {"job_id": upload["upload_id"], "status": "done", "result": RESULT}
        assert transcribe_mock.seen["content"] == b"012345"

    def test_single_request_returns_job(self, client, transcription_jobs, transcribe_mock, prefix):
        response = client.post(
            f"{prefix}/transcribe",
            files={"file": ("video.mp4", io.BytesIO(b"abc"), "video/mp4")},
            data={"language": "pt"},
        )
        assert response.status_code == 202
        assert _poll(client, prefix, response.json()["job_id"])["result"] == RESULT

    def test_failed_job_reports_error(self, client, transcription_jobs, prefix):
        with patch(
            "app.services.transcriptionService.transcribe",
            side_effect=AudioServiceError("O vídeo não possui trilha de áudio"),
        ):
            response = client.post(
                f"{prefix}/transcribe", files={"file": ("mudo.mp4", io.BytesIO(b"x"), "video/mp4")}
            )
            body = _poll(client, prefix, response.json()["job_id"])
        assert body["status"] == "failed"
        assert body["error"] == "O vídeo não possui trilha de áudio"

    def test_invalid_upload_returns_400(self, client, transcription_jobs, prefix):
        response = client.post(
            f"{prefix}/transcribe/uploads", json={"filename": "planilha.xlsx", "size": 4}
        )
        assert response.status_code == 400

    def test_oversized_upload_returns_413(self, client, transcription_jobs, prefix):
        response = client.post(
            f"{prefix}/transcribe/uploads", json={"filename": "video.mp4", "size": 2048}
        )
        assert response.status_code == 413

    def test_wrong_part_length_returns_400(self, client, transcription_jobs, prefix):
        upload = client.post(
            f"{prefix}/transcribe/uploads", json={"filename": "video.mp4", "size": 6}
        ).json()
        response = client.put(
            f"{prefix}/transcribe/uploads/{upload['upload_id']}/parts/0", content=b"01"
        )
        assert response.status_code == 400

    def test_complete_with_missing_parts_returns_409(self, client, transcription_jobs, prefix):
        upload = client.post(
            f"{prefix}/transcribe/uploads", json={"filename": "video.mp4", "size": 6}
        ).json()
        response = client.post(f"{prefix}/transcribe/uploads/{upload['upload_id']}/complete")
        assert response.status_code == 409

    def test_abort_removes_upload(self, client, transcription_jobs, prefix):
        upload = client.post(
            f"{prefix}/transcribe/uploads", json={"filename": "video.mp4", "size": 6}
        ).json()
        base = f"{prefix}/transcribe/uploads/{upload['upload_id']}"
        assert client.delete(base).status_code == 204
        assert client.post(f"{base}/complete").status_code == 404
        assert _leftovers(transcription_jobs) == []

    def test_unknown_job_returns_404(self, client, transcription_jobs, prefix):
        assert client.get(f"{prefix}/transcribe/jobs/nope").status_code == 404

    def test_requires_authentication(self, client_no_auth, transcription_jobs, prefix):
        assert client_no_auth.post(
            f"{prefix}/transcribe/uploads", json={"filename": "video.mp4", "size": 6}
        ).status_code == 401
        assert client_no_auth.get(f"{prefix}/transcribe/jobs/x").status_code == 401

    def test_requires_file_editor_profile(self, client_airline, transcription_jobs, prefix):
        assert client_airline.post(
            f"{prefix}/transcribe/uploads", json={"filename": "video.mp4", "size": 6}
        ).status_code == 403


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def test_startup_wipes_storage_and_shutdown_stops_the_sweeper(tmp_path):
    from app import main

    jobs = TranscriptionJobs(tmp_path / "t", part_size=4, max_size=1024, ttl_seconds=60)
    (jobs.root / "left-by-crash").mkdir(parents=True)
    settings = SimpleNamespace(UPLOAD_DIR=str(tmp_path))

    async def run():
        with patch.object(main, "get_settings", return_value=settings), patch.object(
            main, "get_transcription_jobs", return_value=jobs
        ):
            async with main.lifespan(main.app):
                assert _leftovers(jobs) == []
                sweepers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                assert len(sweepers) == 1
            assert sweepers[0].cancelled()

    asyncio.run(run())
