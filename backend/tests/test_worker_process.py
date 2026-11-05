"""Process-level tests for the worker.

Phase 2 reported that the worker was only ever driven in-process. Phase 3
makes an interruption more expensive, because a half-finished extraction or
transcription has to be cleaned up, so the process boundary is covered here.

The worker is started as a real, separate operating-system process with its
own environment, exactly the way it is started by hand.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.db import create_job, get_job, session
from app.jobs import JobStage, JobStatus, SourceType

BACKEND_DIR = Path(__file__).resolve().parent.parent

#: How long a test waits for the worker to do something.
WAIT_TIMEOUT_SECONDS = 90


def _environment(storage_root: Path, database: Path, **extra: str) -> dict[str, str]:
    """The environment the worker process is started with."""
    environment = dict(os.environ)
    environment.update(
        {
            "CLIPFORGE_STORAGE_DIR": str(storage_root),
            "CLIPFORGE_DATABASE_PATH": str(database),
            "CLIPFORGE_LOG_LEVEL": "INFO",
            "CLIPFORGE_WORKER_POLL_INTERVAL_SECONDS": "0.2",
            "CLIPFORGE_WORKER_LEASE_SECONDS": "10",
            "CLIPFORGE_WORKER_MAX_ATTEMPTS": "1",
            "PYTHONPATH": str(BACKEND_DIR),
        }
    )
    for key in list(environment):
        if key.startswith("CLIPFORGE_") and key not in environment:
            environment.pop(key)
    environment.update(extra)
    return environment


def _wait_for(predicate, *, timeout: int = WAIT_TIMEOUT_SECONDS):  # noqa: ANN001, ANN003
    """Wait until ``predicate`` returns something truthy."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.2)
    return None


def _start_worker(environment: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603 - argument list, never a shell
        [sys.executable, "worker.py"],
        cwd=str(BACKEND_DIR),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _queue_job(database: Path, source: Path, storage_root: Path) -> str:
    """Insert a queued job that points at an existing source file."""
    job_id = "b" * 32
    with session(database) as conn:
        create_job(
            conn,
            job_id=job_id,
            source_type=SourceType.UPLOAD,
            source_reference=source.name,
            source_path=f"sources/{source.name}",
        )
    return job_id


@pytest.fixture()
def worker_world(tmp_path: Path, sample_video: Path):
    """A storage root with a real source video and an empty database."""
    storage_root = tmp_path / "storage"
    sources = storage_root / "sources"
    sources.mkdir(parents=True)
    shutil.copy(sample_video, sources / "clip.mp4")
    database = storage_root / "database" / "clipforge.db"
    return storage_root, database, sources / "clip.mp4"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg is not installed, so the worker cannot extract audio",
)
def test_the_worker_process_claims_and_processes_a_job(worker_world) -> None:
    """A worker that runs as its own process claims the job and works on it."""
    storage_root, database, source = worker_world
    job_id = _queue_job(database, source, storage_root)

    environment = _environment(
        storage_root,
        database,
        # Offline mode with an empty cache: the worker must fail cleanly
        # rather than pretend that it transcribed anything.
        CLIPFORGE_TRANSCRIPTION_OFFLINE="true",
        CLIPFORGE_TRANSCRIPTION_CACHE_DIR=str(storage_root / "models"),
    )
    process = _start_worker(environment)
    try:
        def claimed():
            with session(database) as conn:
                job = get_job(conn, job_id)
            return job is not None and job.worker_id is not None

        assert _wait_for(claimed), "the worker never claimed the job"

        def finished():
            with session(database) as conn:
                job = get_job(conn, job_id)
            return job is not None and job.status in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.QUEUED,
            )

        assert _wait_for(finished), "the worker never finished the job"

        with session(database) as conn:
            job = get_job(conn, job_id)
        assert job is not None
        assert job.attempt_count == 1
        assert job.worker_id is not None
        # Without a model the job cannot be completed, and the failure says
        # exactly why.
        assert job.status is not JobStatus.COMPLETED
        assert job.error_code == "transcription_model_unavailable"
    finally:
        _stop(process)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg is not installed, so the worker cannot extract audio",
)
def test_a_job_left_behind_by_a_dead_worker_is_recovered(worker_world) -> None:
    """A worker that dies leaves a lease behind, and the lease is recovered."""
    storage_root, database, source = worker_world
    job_id = _queue_job(database, source, storage_root)

    # Pretend a worker claimed the job and then died: the lease is in the
    # past and the job is still in an active state.
    with session(database) as conn:
        conn.execute(
            """
            UPDATE jobs
               SET status = ?, stage = ?, worker_id = 'dead-worker',
                   lease_expires_at = '2020-01-01T00:00:00+00:00', attempt_count = 1
             WHERE id = ?
            """,
            (JobStatus.EXTRACTING_AUDIO.value, JobStage.EXTRACTING_AUDIO.value, job_id),
        )

    environment = _environment(
        storage_root,
        database,
        CLIPFORGE_WORKER_MAX_ATTEMPTS="2",
        CLIPFORGE_TRANSCRIPTION_OFFLINE="true",
        CLIPFORGE_TRANSCRIPTION_CACHE_DIR=str(storage_root / "models"),
    )
    process = _start_worker(environment)
    try:

        def taken_over():
            with session(database) as conn:
                job = get_job(conn, job_id)
            return (
                job is not None
                and job.worker_id is not None
                and job.worker_id != "dead-worker"
            )

        assert _wait_for(taken_over), "the dead worker's job was never recovered"

        with session(database) as conn:
            job = get_job(conn, job_id)
        assert job is not None
        # The job was taken over by the new worker and worked on again. It is
        # no longer owned by the worker that died, and it was attempted a
        # second time.
        assert job.worker_id != "dead-worker"
        assert job.attempt_count >= 2
    finally:
        _stop(process)


def test_the_worker_process_leaves_the_database_readable(worker_world) -> None:
    """Stopping the worker must not damage the database."""
    storage_root, database, source = worker_world
    job_id = _queue_job(database, source, storage_root)

    environment = _environment(
        storage_root,
        database,
        CLIPFORGE_TRANSCRIPTION_OFFLINE="true",
        CLIPFORGE_TRANSCRIPTION_CACHE_DIR=str(storage_root / "models"),
    )
    process = _start_worker(environment)
    try:
        def started():
            with session(database) as conn:
                job = get_job(conn, job_id)
            return job is not None and job.worker_id is not None

        assert _wait_for(started, timeout=30), "the worker never started"
    finally:
        _stop(process)

    # The database is still consistent and readable afterwards.
    with session(database) as conn:
        job = get_job(conn, job_id)
        assert job is not None
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def _stop(process: subprocess.Popen) -> None:
    """Stop the worker process and collect its output."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()
            process.wait(timeout=15)
    try:
        output = process.stdout.read().decode("utf-8", "replace") if process.stdout else ""
    except Exception:  # pragma: no cover - defensive
        output = ""
    process.stdout.close() if process.stdout else None
    return output
