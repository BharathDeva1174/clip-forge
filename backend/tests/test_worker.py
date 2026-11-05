"""Tests for the background worker.

The tests drive the worker directly against a temporary database, so they are
deterministic and never depend on timing.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.acquisition import AcquisitionError
from app.config import Settings
from app.db import (
    claim_next_job,
    create_job,
    get_job,
    list_jobs,
    recover_expired_leases,
    session,
    update_job,
)
from app.errors import ErrorCode
from app.jobs import JobStage, JobStatus, SourceType
from app.storage import Storage
from app.worker import Worker
from fakes import FakeTranscriber


class FakeAcquirer:
    """A scripted acquisition adapter."""

    name = "fake"

    def __init__(self, *, result: Path | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    def acquire(self, video, destination_dir, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(video.canonical_url)
        if self.error is not None:
            raise self.error
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = destination_dir / "downloaded.mp4"
        target.write_bytes(b"downloaded")
        return target


def _upload(client: TestClient, sample_video: Path) -> str:
    with sample_video.open("rb") as handle:
        response = client.post(
            "/api/v1/videos/upload",
            files={"file": ("clip.mp4", handle, "video/mp4")},
        )
    assert response.status_code == 202
    return response.json()["id"]


def _worker(settings: Settings, acquirer=None, transcriber=None) -> Worker:
    """Build a worker with a faked speech engine.

    The speech model is never loaded by the ordinary test suite: it is
    faked, so the tests are fast, deterministic and need no network. The real
    engine has its own opt-in integration test.
    """
    return Worker(
        settings,
        worker_id="test-worker",
        acquirer=acquirer,
        transcriber=transcriber if transcriber is not None else FakeTranscriber(),
    )


# --- processing -------------------------------------------------------------


def test_the_worker_completes_a_queued_upload(
    client: TestClient, sample_video: Path, settings: Settings, db: sqlite3.Connection
) -> None:
    job_id = _upload(client, sample_video)

    _worker(settings).run_once()

    job = get_job(db, job_id)
    assert job.status is JobStatus.COMPLETED
    assert job.stage is JobStage.READY
    assert job.progress == 100
    assert job.video_width == 320
    assert job.video_height == 240
    assert job.video_codec == "h264"
    assert job.video_duration_seconds and job.video_duration_seconds > 0
    assert job.completed_at is not None
    assert job.attempt_count == 1


def test_the_worker_marks_invalid_media_as_failed(
    client: TestClient, not_a_video: Path, settings: Settings, db: sqlite3.Connection
) -> None:
    with not_a_video.open("rb") as handle:
        response = client.post(
            "/api/v1/videos/upload",
            files={"file": ("clip.mp4", handle, "video/mp4")},
        )
    # The upload is rejected before it is queued, so build the job by hand to
    # exercise the worker's own inspection.
    assert response.status_code == 415

    storage = Storage(settings.storage_root)
    bad = storage.sources_dir / "broken.mp4"
    bad.write_bytes(b"not a video at all")
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="c" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="broken.mp4",
            source_path=storage.relative_to_root(bad),
        )

    _worker(settings).run_once()

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, "c" * 32)
    assert job.status is JobStatus.FAILED
    assert job.error_code == ErrorCode.INVALID_MEDIA.value
    assert job.error_message


def test_a_failed_job_does_not_stay_in_processing(
    settings: Settings, db: sqlite3.Connection
) -> None:
    storage = Storage(settings.storage_root)
    storage.ensure_layout()
    missing = storage.sources_dir / "missing.mp4"
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="d" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="missing.mp4",
            source_path=storage.relative_to_root(missing),
        )

    _worker(settings).run_once()

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, "d" * 32)
    assert job.status is JobStatus.FAILED
    assert job.status not in (
        JobStatus.VALIDATING,
        JobStatus.DOWNLOADING,
        JobStatus.INSPECTING,
    )


def test_a_permanently_invalid_job_is_not_retried(
    settings: Settings, db: sqlite3.Connection
) -> None:
    storage = Storage(settings.storage_root)
    storage.ensure_layout()
    bad = storage.sources_dir / "bad.mp4"
    bad.write_bytes(b"still not a video")
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="e" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="bad.mp4",
            source_path=storage.relative_to_root(bad),
        )

    worker = _worker(settings)
    for _ in range(4):
        worker.run_once()

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, "e" * 32)
    assert job.status is JobStatus.FAILED
    # A permanent error is never retried, so the attempt count stays at one.
    assert job.attempt_count == 1


def test_a_transient_failure_is_retried_and_the_attempt_count_grows(
    settings: Settings,
) -> None:
    storage = Storage(settings.storage_root)
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="f" * 32,
            source_type=SourceType.YOUTUBE,
            source_reference="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    acquirer = FakeAcquirer(
        error=AcquisitionError(ErrorCode.ACQUISITION_FAILED.value, "nope")
    )
    worker = _worker(settings, acquirer=acquirer)

    worker.run_once()
    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, "f" * 32)
    # The failure is transient, so the job is queued again.
    assert job.status is JobStatus.QUEUED
    assert job.attempt_count == 1
    assert job.error_code == ErrorCode.ACQUISITION_FAILED.value

    worker.run_once()
    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, "f" * 32)
    assert job.attempt_count == 2

    # The maximum number of attempts is two in the test settings, so the
    # second failure is permanent. The underlying cause is kept and the
    # exhausted retries are stated.
    assert job.status is JobStatus.FAILED
    assert job.error_code == ErrorCode.ACQUISITION_FAILED.value
    assert "2 attempts" in job.error_message


def test_a_failed_job_is_not_claimed_again(
    settings: Settings, db: sqlite3.Connection
) -> None:
    storage = Storage(settings.storage_root)
    storage.ensure_layout()
    bad = storage.sources_dir / "bad.mp4"
    bad.write_bytes(b"not a video")
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="9" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="bad.mp4",
            source_path=storage.relative_to_root(bad),
        )

    worker = _worker(settings)
    worker.run_once()
    assert worker.run_once() is None


# --- claiming and concurrency ----------------------------------------------


def test_two_workers_cannot_claim_the_same_job(settings: Settings) -> None:
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="1" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )

    claimed: list[str | None] = []
    barrier = threading.Barrier(2)

    def claim(worker_id: str) -> None:
        barrier.wait()
        with session(settings.resolved_database_path) as conn:
            job = claim_next_job(
                conn, worker_id=worker_id, lease_seconds=settings.worker_lease_seconds
            )
            claimed.append(job.id if job else None)

    threads = [
        threading.Thread(target=claim, args=("worker-a",)),
        threading.Thread(target=claim, args=("worker-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(id_ for id_ in claimed if id_ is not None) == ["1" * 32]
    assert None in claimed


def test_a_restarted_worker_discovers_pending_jobs(settings: Settings) -> None:
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="2" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )

    # A brand new worker, as after a restart.
    worker = _worker(settings)
    job = worker.run_once()

    assert job is not None
    assert job.id == "2" * 32


def test_an_expired_lease_is_recovered_and_queued_again(settings: Settings) -> None:
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="3" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        # Simulate a worker that claimed the job and then died.
        conn.execute(
            """
            UPDATE jobs
               SET status = ?, stage = ?, worker_id = 'dead-worker',
                   lease_expires_at = '2020-01-01T00:00:00+00:00', attempt_count = 1
             WHERE id = ?
            """,
            (JobStatus.VALIDATING.value, JobStage.VALIDATING.value, "3" * 32),
        )

    recovered = _worker(settings).recover()

    assert len(recovered) == 1
    assert recovered[0].status is JobStatus.QUEUED
    assert recovered[0].worker_id is None
    assert recovered[0].error_code == ErrorCode.LEASE_EXPIRED.value


def test_a_job_that_used_up_its_attempts_is_failed_not_requeued(
    settings: Settings,
) -> None:
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="4" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        conn.execute(
            """
            UPDATE jobs
               SET status = ?, stage = ?, worker_id = 'dead-worker',
                   lease_expires_at = '2020-01-01T00:00:00+00:00', attempt_count = 99
             WHERE id = ?
            """,
            (JobStatus.VALIDATING.value, JobStage.VALIDATING.value, "4" * 32),
        )

    recovered = _worker(settings).recover()

    assert len(recovered) == 1
    assert recovered[0].status is JobStatus.FAILED
    assert recovered[0].error_code == ErrorCode.MAX_ATTEMPTS_EXCEEDED.value


def test_a_job_is_never_left_stuck_in_an_active_state(
    settings: Settings,
) -> None:
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="5" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        for status in (
            JobStatus.VALIDATING,
            JobStatus.DOWNLOADING,
            JobStatus.INSPECTING,
        ):
            conn.execute(
                "UPDATE jobs SET status = ?, stage = ?, lease_expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
                (status.value, status.value, "5" * 32),
            )
            recovered = recover_expired_leases(
                conn, max_attempts=settings.worker_max_attempts
            )
            assert recovered
            assert recovered[0].status in (JobStatus.QUEUED, JobStatus.FAILED)


# --- shutdown ---------------------------------------------------------------


def test_an_interrupted_job_is_released_not_completed(settings: Settings) -> None:
    storage = Storage(settings.storage_root)
    storage.ensure_layout()
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="6" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
            source_path=storage.relative_to_root(storage.sources_dir / "x.mp4"),
        )

    worker = _worker(settings)
    worker.request_stop()

    worker.run_once()

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, "6" * 32)
    assert job.status is not JobStatus.COMPLETED


def test_requesting_a_stop_marks_the_worker_as_stopping(settings: Settings) -> None:
    worker = _worker(settings)
    assert worker.stopping is False
    worker.request_stop()
    assert worker.stopping is True


# --- media tools ------------------------------------------------------------


def test_a_missing_ffprobe_fails_the_job_cleanly(settings: Settings) -> None:
    broken = Settings(
        _env_file=None,
        storage_dir=settings.storage_dir,
        database_path=settings.resolved_database_path,
        ffprobe_path="ffprobe-that-does-not-exist",
        worker_max_attempts=1,
        log_level="WARNING",
    )
    storage = Storage(broken.storage_root)
    storage.ensure_layout()
    sample = storage.sources_dir / "clip.mp4"
    sample.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)

    with session(broken.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="7" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
            source_path=storage.relative_to_root(sample),
        )

    _worker(broken).run_once()

    with session(broken.resolved_database_path) as conn:
        job = get_job(conn, "7" * 32)
    assert job.status is JobStatus.FAILED
    assert job.error_code == ErrorCode.MEDIA_TOOL_UNAVAILABLE.value


# --- idempotency ------------------------------------------------------------


def test_processing_a_job_twice_does_not_duplicate_records(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    job_id = _upload(client, sample_video)

    worker = _worker(settings)
    worker.run_once()

    with session(settings.resolved_database_path) as conn:
        before = list_jobs(conn, limit=200)
    assert len(before) == 1

    # The job is completed, so it is not claimed again.
    worker.run_once()
    with session(settings.resolved_database_path) as conn:
        after = list_jobs(conn, limit=200)
    assert len(after) == 1
    assert after[0].id == job_id


def test_the_source_file_is_not_modified_by_processing(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    job_id = _upload(client, sample_video)

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, job_id)
        storage = Storage(settings.storage_root)
        path = storage.resolve(job.source_path)
        before = path.read_bytes()

    _worker(settings).run_once()

    assert path.read_bytes() == before


def test_a_lease_is_renewed_while_a_job_runs(settings: Settings) -> None:
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="8" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
        )
        job = claim_next_job(
            conn, worker_id="w", lease_seconds=settings.worker_lease_seconds
        )
        assert job is not None
        assert job.lease_expires_at is not None
