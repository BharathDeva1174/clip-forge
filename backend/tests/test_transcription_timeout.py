"""Process-level tests for the transcription timeout.

These are the tests that back the hard-timeout claim. A worker runs as a
real, separate operating-system process; the inference it starts is a real
child process; and the inference is made to stall on purpose.

The claims under test are:

* a stalled inference is stopped, and the job ends in a terminal state with
  the documented error code rather than staying ``transcribing`` for ever;
* the worker survives the timeout and goes on to the next job;
* no child process and no temporary artefact is left behind.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.db import create_job, get_job, get_transcript, session
from app.jobs import TERMINAL_STATUSES, JobStatus, SourceType

BACKEND_DIR = Path(__file__).resolve().parent.parent

#: How long a test waits for the worker to do something.
WAIT_TIMEOUT_SECONDS = 120


def _environment(storage_root: Path, database: Path, **extra: str) -> dict[str, str]:
    """The environment the worker process is started with."""
    environment = dict(os.environ)
    environment.update(
        {
            "CLIPFORGE_STORAGE_DIR": str(storage_root),
            "CLIPFORGE_DATABASE_PATH": str(database),
            "CLIPFORGE_LOG_LEVEL": "INFO",
            "CLIPFORGE_WORKER_POLL_INTERVAL_SECONDS": "0.2",
            "CLIPFORGE_WORKER_LEASE_SECONDS": "30",
            "CLIPFORGE_WORKER_MAX_ATTEMPTS": "1",
            "CLIPFORGE_TRANSCRIPTION_CACHE_DIR": str(storage_root / "models"),
            "PYTHONPATH": str(BACKEND_DIR),
        }
    )
    environment.update(extra)
    return environment


def _start_worker(environment: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603 - argument list, never a shell
        [sys.executable, str(BACKEND_DIR / "tests" / "worker_stub.py")],
        cwd=str(BACKEND_DIR),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _queue_job(database: Path, source: Path, job_id: str) -> str:
    """Insert a queued job that points at an existing source file."""
    with session(database) as conn:
        create_job(
            conn,
            job_id=job_id,
            source_type=SourceType.UPLOAD,
            source_reference=source.name,
            source_path=f"sources/{source.name}",
        )
    return job_id


def _wait_for(predicate, *, timeout: int = WAIT_TIMEOUT_SECONDS):  # noqa: ANN001, ANN003
    """Wait until ``predicate`` returns something truthy."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.2)
    return None


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
    if process.stdout:
        process.stdout.close()
    return output


def _child_pids(parent_pid: int) -> list[int]:
    """The process ids of the direct children of ``parent_pid``."""
    if os.name == "nt":
        script = (
            f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={parent_pid}')"
            ".ProcessId"
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return [int(part) for part in completed.stdout.split() if part.strip().isdigit()]

    completed = subprocess.run(
        ["ps", "-o", "pid=", "--ppid", str(parent_pid)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return [int(part) for part in completed.split() if part.strip().isdigit()]


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
def test_a_stalled_inference_is_stopped_and_the_worker_survives(worker_world) -> None:
    """The headline claim: a stalled inference cannot hold the worker."""
    storage_root, database, source = worker_world
    first_job = _queue_job(database, source, "a" * 32)
    second_job = _queue_job(database, source, "b" * 32)

    environment = _environment(
        storage_root,
        database,
        # The inference stalls the first time and then behaves.
        CLIPFORGE_TEST_HOST_MODE="stall_once",
        CLIPFORGE_TEST_HOST_STATE=str(storage_root / "stalled.marker"),
        CLIPFORGE_TEST_HOST_PIDFILE=str(storage_root / "host-pids.txt"),
        CLIPFORGE_TRANSCRIPTION_TIMEOUT_SECONDS="5",
    )
    process = _start_worker(environment)
    try:
        def failed():
            with session(database) as conn:
                job = get_job(conn, first_job)
            return job is not None and job.status is JobStatus.FAILED

        assert _wait_for(failed), "the stalled inference was never stopped"

        with session(database) as conn:
            job = get_job(conn, first_job)
        assert job is not None
        # A terminal state with the documented code, not an endless
        # "transcribing".
        assert job.error_code == "transcription_timeout"
        assert job.status is JobStatus.FAILED
        assert job.status in TERMINAL_STATUSES
        # A timed-out transcription does not commit a partial transcript.
        with session(database) as conn:
            assert get_transcript(conn, first_job) is None

        # The worker is still alive and able to work.
        assert process.poll() is None

        def completed():
            with session(database) as conn:
                job = get_job(conn, second_job)
            return job is not None and job.status is JobStatus.COMPLETED

        assert _wait_for(completed), "the worker did not process the next job"

        with session(database) as conn:
            job = get_job(conn, second_job)
            transcript = get_transcript(conn, second_job)
        assert job is not None
        assert job.status is JobStatus.COMPLETED
        assert transcript is not None
        # The stub host reports two segments, but the second one lies past the
        # end of the one-second test video, so it is dropped: the transcript
        # never claims speech that is outside the audio.
        assert transcript.segment_count == 1
        assert transcript.duration_seconds == pytest.approx(1.0, abs=0.05)

        # The stalled child was stopped, so only the child that is doing the
        # current work is left.
        children = _child_pids(process.pid)
        assert len(children) <= 1
    finally:
        _stop(process)

    # When the worker stops, its children stop with it and the scratch space
    # is empty.
    assert _child_pids(process.pid) == []
    temp = storage_root / "temp"
    leftovers = list(temp.glob("audio-*")) if temp.is_dir() else []
    assert leftovers == []


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg is not installed, so the worker cannot extract audio",
)
def test_the_worker_releases_the_job_when_it_is_stopped_mid_transcription(
    worker_world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A graceful shutdown during a transcription releases the job.

    The worker stops the inference process from inside the progress
    callback, so this is the path where a lock mistake would deadlock the
    worker.
    """
    storage_root, database, source = worker_world
    job_id = _queue_job(database, source, "d" * 32)
    monkeypatch.setenv("CLIPFORGE_TEST_HOST_MODE", "slow")
    monkeypatch.setenv("CLIPFORGE_TEST_HOST_DELAY", "60")

    from app.config import Settings  # noqa: PLC0415
    from app.transcription import (  # noqa: PLC0415
        TranscriptionSettings,
        resolve_cache_dir,
    )
    from app.transcription_process import (  # noqa: PLC0415
        ProcessTranscriptionEngine,
    )
    from app.worker import Worker  # noqa: PLC0415

    settings = Settings(
        _env_file=None,
        storage_dir=storage_root,
        database_path=database,
        worker_poll_interval_seconds=0.05,
        worker_lease_seconds=30,
        worker_max_attempts=1,
        log_level="WARNING",
    )
    transcription_settings = TranscriptionSettings(_env_file=None, timeout_seconds=300)
    engine = ProcessTranscriptionEngine(
        transcription_settings,
        cache_dir=resolve_cache_dir(transcription_settings, storage_root),
        host_path=Path(__file__).resolve().parent / "host_stub.py",
    )
    worker = Worker(settings, worker_id="test-worker", transcriber=engine)

    thread = threading.Thread(target=worker.run_once, daemon=True)
    thread.start()
    try:
        def transcribing():
            with session(database) as conn:
                job = get_job(conn, job_id)
            return job is not None and job.status is JobStatus.TRANSCRIBING

        assert _wait_for(transcribing), "the worker never started transcribing"

        # The worker is asked to stop while the transcription is running.
        worker.request_stop()
        thread.join(timeout=60)
        assert not thread.is_alive(), "the worker did not stop"
    finally:
        engine.terminate()

    with session(database) as conn:
        job = get_job(conn, job_id)
        assert job is not None
        # The job was not failed and not left transcribing: it was released.
        assert job.status is JobStatus.QUEUED
        assert get_transcript(conn, job_id) is None


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg is not installed, so the worker cannot extract audio",
)
def test_the_worker_stops_its_inference_process_when_it_is_shut_down(
    worker_world,
) -> None:
    """A graceful shutdown does not leave an inference process behind."""
    storage_root, database, source = worker_world
    job_id = _queue_job(database, source, "c" * 32)

    environment = _environment(
        storage_root,
        database,
        CLIPFORGE_TEST_HOST_MODE="stall",
        CLIPFORGE_TEST_HOST_PIDFILE=str(storage_root / "host-pids.txt"),
        CLIPFORGE_TRANSCRIPTION_TIMEOUT_SECONDS="300",
    )
    process = _start_worker(environment)
    try:
        def transcribing():
            with session(database) as conn:
                job = get_job(conn, job_id)
            return job is not None and job.status is JobStatus.TRANSCRIBING

        assert _wait_for(transcribing), "the worker never started transcribing"

        # The inference process exists while the work is running.
        def child_exists():
            return bool(_child_pids(process.pid))

        assert _wait_for(child_exists, timeout=60), "no inference process was started"
    finally:
        _stop(process)

    # After the shutdown the inference process is gone.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and _child_pids(process.pid):
        time.sleep(0.5)
    assert _child_pids(process.pid) == []
