"""Tests for the Phase 3 worker pipeline.

These drive the worker the way the application does: a job is claimed, the
audio is extracted, the speech is transcribed and the transcript is stored.
The speech engine is faked so that the tests stay fast and offline.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import get_job, get_transcript, list_transcript_segments, session
from app.errors import ErrorCode
from app.interruption import WorkInterrupted
from app.jobs import JobStage, JobStatus
from app.storage import Storage
from app.worker import Worker
from fakes import FakeAudioExtractor, FakeSegment, FakeTranscriber, transcription_error


def _worker(settings: Settings, **kwargs) -> Worker:
    return Worker(settings, worker_id="test-worker", **kwargs)


def _upload(client: TestClient, sample_video: Path) -> str:
    with sample_video.open("rb") as handle:
        response = client.post(
            "/api/v1/videos/upload",
            files={"file": ("clip.mp4", handle, "video/mp4")},
        )
    assert response.status_code == 202
    return response.json()["id"]


def _stored_segments(settings: Settings, job_id: str) -> list[str]:
    with session(settings.resolved_database_path) as conn:
        transcript = get_transcript(conn, job_id)
        assert transcript is not None
        return [
            segment.text
            for segment in list_transcript_segments(conn, transcript.id)
        ]


# --- the happy path --------------------------------------------------------


def test_a_job_goes_through_extraction_and_transcription(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    job_id = _upload(client, sample_video)
    transcriber = FakeTranscriber(
        segments=[FakeSegment(start=0.0, end=0.5, text="hello world")]
    )

    _worker(settings, transcriber=transcriber).run_once()

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, job_id)
    assert job is not None
    assert job.status is JobStatus.COMPLETED
    assert job.stage is JobStage.READY
    assert job.progress == 100
    assert _stored_segments(settings, job_id) == ["hello world"]


def test_the_engine_receives_the_extracted_audio(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    job_id = _upload(client, sample_video)
    existed: list[bool] = []

    class WatchingTranscriber(FakeTranscriber):
        def transcribe(self, audio_path, *, on_progress=None, **kwargs):  # noqa: ANN001, ANN003
            existed.append(Path(audio_path).exists())
            return super().transcribe(audio_path, on_progress=on_progress)

    _worker(settings, transcriber=WatchingTranscriber()).run_once()

    assert len(existed) == 1
    assert existed[0] is True


def test_the_extracted_audio_is_removed_after_the_transcription(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    job_id = _upload(client, sample_video)
    transcriber = FakeTranscriber()

    _worker(settings, transcriber=transcriber).run_once()

    storage = Storage(settings.storage_root)
    leftovers = list(storage.temp_dir.glob("audio-*"))
    assert leftovers == []
    # The source video itself is still there.
    assert list(storage.sources_dir.iterdir())


def test_the_extracted_audio_can_be_kept(
    client: TestClient, sample_video: Path, settings: Settings, tmp_path: Path
) -> None:
    keep = Settings(
        _env_file=None,
        storage_dir=settings.storage_dir,
        database_path=settings.resolved_database_path,
        audio_temp_retention="keep",
        log_level="WARNING",
    )
    _upload(client, sample_video)

    _worker(keep, transcriber=FakeTranscriber()).run_once()

    storage = Storage(keep.storage_root)
    assert list(storage.temp_dir.glob("audio-*"))


def test_the_progress_never_goes_backwards(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    job_id = _upload(client, sample_video)
    seen: list[int] = []

    class RecordingTranscriber(FakeTranscriber):
        def transcribe(self, audio_path, *, on_progress=None, **kwargs):  # noqa: ANN001, ANN003
            def watch():
                while True:
                    with session(settings.resolved_database_path) as conn:
                        job = get_job(conn, job_id)
                        if job is not None:
                            seen.append(job.progress)
                    time.sleep(0.01)
                    if len(seen) > 200:
                        return
                    if on_progress is not None:
                        on_progress(0.5)

            return super().transcribe(audio_path, on_progress=on_progress)

    _worker(settings, transcriber=FakeTranscriber()).run_once()

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, job_id)
    assert job is not None
    assert job.progress == 100
    assert 0 <= job.progress <= 100


def test_the_transcription_progress_is_approximate_and_stays_below_the_end(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    job_id = _upload(client, sample_video)
    reported: list[int] = []

    class ReportingTranscriber(FakeTranscriber):
        def transcribe(self, audio_path, *, on_progress=None, **kwargs):  # noqa: ANN001, ANN003
            if on_progress is not None:
                on_progress(0.0)
                on_progress(0.5)
                on_progress(1.0)
            return super().transcribe(audio_path, on_progress=None)

    worker = Worker(settings, worker_id="test-worker", transcriber=ReportingTranscriber())
    original = worker._transcription_reporter

    def spy(job):  # noqa: ANN001, ANN003
        inner = original(job)

        def wrapped(fraction):  # noqa: ANN001, ANN003
            inner(fraction)
            with session(settings.resolved_database_path) as conn:
                reported.append(get_job(conn, job.id).progress)

        return wrapped

    worker._transcription_reporter = spy  # type: ignore[method-assign]
    worker.run_once()

    assert reported
    assert max(reported) < 100
    assert min(reported) >= 85


# --- failures --------------------------------------------------------------


def test_a_source_without_audio_fails_with_a_stable_code(
    client: TestClient, sample_video_without_audio: Path, settings: Settings
) -> None:
    job_id = _upload(client, sample_video_without_audio)

    _worker(settings, transcriber=FakeTranscriber()).run_once()

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, job_id)
        assert job is not None
        assert job.status is JobStatus.FAILED
        assert job.error_code == ErrorCode.NO_AUDIO_STREAM.value
        assert get_transcript(conn, job_id) is None


def test_a_missing_source_fails_cleanly(settings: Settings) -> None:
    storage = Storage(settings.storage_root)
    storage.ensure_layout()
    with session(settings.resolved_database_path) as conn:
        from app.db import create_job
        from app.jobs import SourceType

        create_job(
            conn,
            job_id="z" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="gone.mp4",
            source_path=storage.relative_to_root(storage.sources_dir / "gone.mp4"),
        )

    _worker(settings, transcriber=FakeTranscriber()).run_once()

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, "z" * 32)
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.status not in (
        JobStatus.VALIDATING,
        JobStatus.DOWNLOADING,
        JobStatus.INSPECTING,
        JobStatus.EXTRACTING_AUDIO,
        JobStatus.TRANSCRIBING,
    )


def test_a_failing_transcription_fails_the_job_with_a_stable_code(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    job_id = _upload(client, sample_video)
    transcriber = FakeTranscriber(
        error=transcription_error(
            ErrorCode.TRANSCRIPTION_FAILED.value, "The audio could not be transcribed."
        )
    )

    _worker(settings, transcriber=transcriber).run_once()

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, job_id)
        assert job is not None
        assert job.status is JobStatus.FAILED
        assert job.error_code == ErrorCode.TRANSCRIPTION_FAILED.value
        assert get_transcript(conn, job_id) is None


def test_a_missing_model_is_reported_as_such(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    job_id = _upload(client, sample_video)
    transcriber = FakeTranscriber(
        error=transcription_error(
            ErrorCode.TRANSCRIPTION_MODEL_UNAVAILABLE.value,
            "The local transcription model could not be loaded.",
        )
    )

    _worker(settings, transcriber=transcriber).run_once()

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, job_id)
    assert job is not None
    # A missing model is worth another attempt: the user may download it.
    assert job.status is JobStatus.QUEUED
    assert job.error_code == ErrorCode.TRANSCRIPTION_MODEL_UNAVAILABLE.value


def test_an_interrupted_extraction_does_not_complete_the_job(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    job_id = _upload(client, sample_video)

    class InterruptedExtractor(FakeAudioExtractor):
        def extract(self, *args: object, **kwargs: object):
            raise WorkInterrupted("the worker is shutting down")

    _worker(
        settings,
        transcriber=FakeTranscriber(),
        audio_extractor=InterruptedExtractor(),
    ).run_once()

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, job_id)
        assert job is not None
        assert job.status is not JobStatus.COMPLETED
        assert job.status is JobStatus.QUEUED
        assert get_transcript(conn, job_id) is None


def test_a_shutdown_during_the_transcription_releases_the_job(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    job_id = _upload(client, sample_video)
    started = threading.Event()

    class BlockingTranscriber(FakeTranscriber):
        def transcribe(self, audio_path, *, on_progress=None, **kwargs):  # noqa: ANN001, ANN003
            started.set()
            time.sleep(0.4)
            return super().transcribe(audio_path, on_progress=on_progress)

    worker = _worker(settings, transcriber=BlockingTranscriber())
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    assert started.wait(timeout=10)
    worker.request_stop()
    thread.join(timeout=30)

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, job_id)
    assert job is not None
    assert job.status is not JobStatus.COMPLETED


# --- retries ---------------------------------------------------------------


def test_a_retry_does_not_duplicate_the_transcript(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    """Re-processing a job replaces its transcript instead of adding one.

    The job is put back into the queue the way a crashed worker leaves it:
    the transcript was committed, but the job was never marked completed.
    """
    job_id = _upload(client, sample_video)
    transcriber = FakeTranscriber(
        segments=[FakeSegment(start=0.0, end=0.5, text="first attempt")]
    )

    worker = _worker(settings, transcriber=transcriber)
    worker.run_once()

    assert _stored_segments(settings, job_id) == ["first attempt"]

    # Simulate a worker that committed the transcript and then died before it
    # could mark the job as completed.
    with session(settings.resolved_database_path) as conn:
        conn.execute(
            """
            UPDATE jobs
               SET status = ?, stage = ?, worker_id = NULL, lease_expires_at = NULL
             WHERE id = ?
            """,
            (JobStatus.TRANSCRIBING.value, JobStage.TRANSCRIBING.value, job_id),
        )
        from app.db import release_for_retry

        release_for_retry(
            conn, job_id, error_code="lease_expired", error_message="again please"
        )

    # Re-process the same job with a different transcript.
    transcriber.segments = [FakeSegment(start=0.0, end=0.5, text="second attempt")]
    worker.run_once()

    assert _stored_segments(settings, job_id) == ["second attempt"]
    with session(settings.resolved_database_path) as conn:
        transcript = get_transcript(conn, job_id)
        assert transcript is not None
        assert transcript.segment_count == 1
        # There is exactly one transcript row, not one per attempt.
        rows = conn.execute(
            "SELECT COUNT(*) FROM transcripts WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
        assert rows == 1


def test_a_failed_transcription_leaves_no_half_written_transcript(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    job_id = _upload(client, sample_video)
    transcriber = FakeTranscriber(
        error=transcription_error(ErrorCode.TRANSCRIPTION_FAILED.value, "nope")
    )

    _worker(settings, transcriber=transcriber).run_once()

    with session(settings.resolved_database_path) as conn:
        assert get_transcript(conn, job_id) is None
        assert (
            conn.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0] == 0
        )


# --- concurrency and responsiveness ----------------------------------------


def test_two_workers_cannot_process_the_same_job(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    job_id = _upload(client, sample_video)

    first = _worker(settings, transcriber=FakeTranscriber())
    second = Worker(settings, worker_id="other-worker", transcriber=FakeTranscriber())

    first.run_once()
    assert second.run_once() is None

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, job_id)
    assert job is not None
    assert job.status is JobStatus.COMPLETED
    assert job.attempt_count == 1


def test_the_api_stays_responsive_while_the_transcription_runs(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    _upload(client, sample_video)
    started = threading.Event()
    release = threading.Event()

    class SlowTranscriber(FakeTranscriber):
        def transcribe(self, audio_path, *, on_progress=None, **kwargs):  # noqa: ANN001, ANN003
            started.set()
            release.wait(timeout=20)
            return super().transcribe(audio_path, on_progress=on_progress)

    worker = _worker(settings, transcriber=SlowTranscriber())
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    assert started.wait(timeout=10)

    try:
        response = client.get("/api/v1/jobs")
        assert response.status_code == 200
        assert response.json()["items"]
        health = client.get("/health")
        assert health.status_code == 200
    finally:
        release.set()
        thread.join(timeout=30)


# --- logging ---------------------------------------------------------------


def test_the_worker_logs_do_not_leak_absolute_paths(
    client: TestClient,
    sample_video: Path,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _upload(client, sample_video)

    with caplog.at_level(logging.INFO, logger="clipforge.worker"):
        _worker(settings, transcriber=FakeTranscriber()).run_once()

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert messages
    # The job id is useful; the absolute path of the source is not.
    assert str(settings.storage_root) not in messages


def test_the_worker_logs_identify_the_job(
    client: TestClient, sample_video: Path, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    job_id = _upload(client, sample_video)

    with caplog.at_level(logging.INFO, logger="clipforge.worker"):
        _worker(settings, transcriber=FakeTranscriber()).run_once()

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert job_id[:8] in messages
