"""Job lifecycle, recovery and database integrity.

These tests are about what happens when things go wrong: a worker dies, a
lease expires, a second worker takes over, a migration fails halfway. The
database is always a throwaway one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import (
    SCHEMA_VERSION,
    claim_next_job,
    create_job,
    get_job,
    get_transcript,
    list_transcript_segments,
    recover_expired_leases,
    release_for_retry,
    save_transcript,
    session,
    update_job,
)
from app.jobs import JobStage, JobStatus, SourceType
from app.transcripts import Transcript, TranscriptSegment, validate_segments
from app.worker import Worker
from fakes import FakeSegment, FakeTranscriber


def _transcript(text: str) -> Transcript:
    return Transcript(
        language="en",
        model_name="fake",
        audio_sample_rate=16000,
        audio_channels=1,
        duration_seconds=10.0,
        segments=tuple(
            validate_segments(
                [FakeSegment(start=0.0, end=1.0, text=text)], duration_seconds=10.0
            )
        ),
    )


def _queue_job(settings: Settings, job_id: str) -> None:
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id=job_id,
            source_type=SourceType.UPLOAD,
            source_reference="clip.mp4",
            source_path="sources/clip.mp4",
        )


def _walk_to_completed(conn: sqlite3.Connection, job_id: str) -> None:
    """Move a job through the whole pipeline, as the worker does."""
    for status, stage in (
        (JobStatus.VALIDATING, JobStage.VALIDATING),
        (JobStatus.INSPECTING, JobStage.INSPECTING),
        (JobStatus.EXTRACTING_AUDIO, JobStage.EXTRACTING_AUDIO),
        (JobStatus.TRANSCRIBING, JobStage.TRANSCRIBING),
        (JobStatus.COMPLETED, JobStage.READY),
    ):
        update_job(conn, job_id, status=status, stage=stage)


# --- a job only completes when its transcript is stored ----------------------


def test_a_job_cannot_go_from_inspecting_to_completed(db: sqlite3.Connection) -> None:
    """The state machine itself refuses the shortcut."""
    from app.jobs import InvalidTransitionError

    create_job(
        db,
        job_id="a" * 32,
        source_type=SourceType.UPLOAD,
        source_reference="clip.mp4",
    )
    update_job(db, "a" * 32, status=JobStatus.VALIDATING)
    update_job(db, "a" * 32, status=JobStatus.INSPECTING)

    with pytest.raises(InvalidTransitionError):
        update_job(db, "a" * 32, status=JobStatus.COMPLETED)


def test_a_completed_job_always_has_its_transcript(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    with sample_video.open("rb") as handle:
        response = client.post(
            "/api/v1/videos/upload",
            files={"file": ("clip.mp4", handle, "video/mp4")},
        )
    job_id = response.json()["id"]

    Worker(settings, worker_id="w1", transcriber=FakeTranscriber()).run_once()

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, job_id)
        transcript = get_transcript(conn, job_id)
    assert job is not None
    assert job.status is JobStatus.COMPLETED
    assert transcript is not None


def test_a_failed_transcription_leaves_no_transcript_and_no_completed_job(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    from app.errors import ErrorCode
    from app.transcription import TranscriptionError

    with sample_video.open("rb") as handle:
        response = client.post(
            "/api/v1/videos/upload",
            files={"file": ("clip.mp4", handle, "video/mp4")},
        )
    job_id = response.json()["id"]

    transcriber = FakeTranscriber(
        error=TranscriptionError(ErrorCode.TRANSCRIPTION_FAILED.value, "nope")
    )
    Worker(settings, worker_id="w1", transcriber=transcriber).run_once()

    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, job_id)
        assert job is not None
        assert job.status is JobStatus.FAILED
        assert get_transcript(conn, job_id) is None
        assert (
            conn.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0]
            == 0
        )


# --- a stale worker must not overwrite a newer attempt -----------------------


def test_a_stale_worker_cannot_replace_the_transcript_of_a_newer_attempt(
    settings: Settings,
) -> None:
    """The result that stands is the one from the attempt that owns the job."""
    _queue_job(settings, "b" * 32)

    with session(settings.resolved_database_path) as conn:
        first = claim_next_job(conn, worker_id="worker-a", lease_seconds=300)
        assert first is not None
        stale_job = first

        # The lease of worker A expires and the job is recovered.
        conn.execute(
            "UPDATE jobs SET lease_expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            ("b" * 32,),
        )
        recovered = recover_expired_leases(conn, max_attempts=5)
        assert [job.id for job in recovered] == ["b" * 32]

        # Worker B takes the job over and finishes it.
        second = claim_next_job(conn, worker_id="worker-b", lease_seconds=300)
        assert second is not None
        save_transcript(
            conn, job_id="b" * 32, transcript=_transcript("from worker b")
        )
        _walk_to_completed(conn, "b" * 32)

        # Worker A comes back with its own, older, result.
        stored = save_transcript(
            conn,
            job_id="b" * 32,
            transcript=_transcript("from worker a"),
            expected_worker_id="worker-a",
        )
        assert stored is None

        transcript = get_transcript(conn, "b" * 32)
        assert transcript is not None
        segments = list_transcript_segments(conn, transcript.id)
        assert [segment.text for segment in segments] == ["from worker b"]


def test_a_stale_worker_cannot_fail_a_job_that_somebody_else_owns(
    settings: Settings,
) -> None:
    _queue_job(settings, "c" * 32)

    with session(settings.resolved_database_path) as conn:
        first = claim_next_job(conn, worker_id="worker-a", lease_seconds=300)
        assert first is not None
        conn.execute(
            "UPDATE jobs SET lease_expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            ("c" * 32,),
        )
        recover_expired_leases(conn, max_attempts=5)
        second = claim_next_job(conn, worker_id="worker-b", lease_seconds=300)
        assert second is not None

        # Worker A tries to fail the job it no longer owns.
        result = update_job(
            conn,
            "c" * 32,
            status=JobStatus.FAILED,
            stage=JobStage.FAILED,
            error_code="transcription_failed",
            error_message="stale",
            expected_worker_id="worker-a",
        )

        assert result is None
        job = get_job(conn, "c" * 32)
        assert job is not None
        assert job.status is JobStatus.VALIDATING
        assert job.error_code is None


def test_a_worker_that_still_owns_the_job_can_still_write(settings: Settings) -> None:
    _queue_job(settings, "d" * 32)

    with session(settings.resolved_database_path) as conn:
        claimed = claim_next_job(conn, worker_id="worker-a", lease_seconds=300)
        assert claimed is not None

        result = update_job(
            conn,
            "d" * 32,
            progress=50,
            expected_worker_id="worker-a",
        )

        assert result is not None
        assert result.progress == 50


# --- lease recovery and retries are idempotent -------------------------------


def test_recovering_a_lease_does_not_duplicate_anything(settings: Settings) -> None:
    _queue_job(settings, "e" * 32)

    with session(settings.resolved_database_path) as conn:
        claimed = claim_next_job(conn, worker_id="worker-a", lease_seconds=300)
        assert claimed is not None
        save_transcript(conn, job_id="e" * 32, transcript=_transcript("first"))
        conn.execute(
            "UPDATE jobs SET lease_expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            ("e" * 32,),
        )

        recovered = recover_expired_leases(conn, max_attempts=5)
        assert len(recovered) == 1
        # The job is back in the queue, and its transcript is untouched.
        job = get_job(conn, "e" * 32)
        assert job is not None
        assert job.status is JobStatus.QUEUED
        transcript = get_transcript(conn, "e" * 32)
        assert transcript is not None
        assert transcript.segment_count == 1

        # The job is claimed again and transcribed again: still one transcript.
        again = claim_next_job(conn, worker_id="worker-b", lease_seconds=300)
        assert again is not None
        save_transcript(conn, job_id="e" * 32, transcript=_transcript("second"))

        rows = conn.execute(
            "SELECT COUNT(*) FROM transcripts WHERE job_id = ?", ("e" * 32,)
        ).fetchone()[0]
        assert rows == 1
        transcript = get_transcript(conn, "e" * 32)
        assert transcript is not None
        segments = list_transcript_segments(conn, transcript.id)
        assert [segment.text for segment in segments] == ["second"]


def test_a_terminal_job_is_not_put_back_on_the_queue(settings: Settings) -> None:
    """Terminal states stay terminal."""
    _queue_job(settings, "f" * 32)

    with session(settings.resolved_database_path) as conn:
        _walk_to_completed(conn, "f" * 32)
        job = get_job(conn, "f" * 32)
        assert job is not None
        assert job.status is JobStatus.COMPLETED

        release_for_retry(
            conn, "f" * 32, error_code="lease_expired", error_message="again"
        )

        job = get_job(conn, "f" * 32)
        assert job is not None
        assert job.status is JobStatus.COMPLETED
        assert job.stage is JobStage.READY


def test_a_failed_job_is_not_put_back_on_the_queue(settings: Settings) -> None:
    _queue_job(settings, "9" * 32)

    with session(settings.resolved_database_path) as conn:
        update_job(conn, "9" * 32, status=JobStatus.VALIDATING)
        update_job(
            conn,
            "9" * 32,
            status=JobStatus.FAILED,
            stage=JobStage.FAILED,
            error_code="invalid_media",
            error_message="nope",
        )

        release_for_retry(
            conn, "9" * 32, error_code="lease_expired", error_message="again"
        )

        job = get_job(conn, "9" * 32)
        assert job is not None
        assert job.status is JobStatus.FAILED
        assert job.error_code == "invalid_media"


def test_a_job_whose_attempts_are_used_up_is_failed_permanently(
    settings: Settings,
) -> None:
    _queue_job(settings, "8" * 32)

    with session(settings.resolved_database_path) as conn:
        claimed = claim_next_job(conn, worker_id="worker-a", lease_seconds=300)
        assert claimed is not None
        conn.execute(
            "UPDATE jobs SET lease_expires_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            ("8" * 32,),
        )

        recovered = recover_expired_leases(conn, max_attempts=1)

        assert len(recovered) == 1
        job = get_job(conn, "8" * 32)
        assert job is not None
        assert job.status is JobStatus.FAILED
        assert job.error_code == "max_attempts_exceeded"


# --- legacy jobs ------------------------------------------------------------


def test_a_legacy_completed_job_has_no_transcript_and_says_so(
    client: TestClient, settings: Settings
) -> None:
    """A job finished before transcripts existed is completed, not transcribed."""
    with session(settings.resolved_database_path) as conn:
        create_job(
            conn,
            job_id="7" * 32,
            source_type=SourceType.UPLOAD,
            source_reference="old.mp4",
            source_path="sources/old.mp4",
        )
        _walk_to_completed(conn, "7" * 32)

    response = client.get(f"/api/v1/jobs/{'7' * 32}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["has_transcript"] is False

    transcript = client.get(f"/api/v1/jobs/{'7' * 32}/transcript")
    assert transcript.status_code == 200
    assert transcript.json()["status"] == "pending"
    assert transcript.json()["segments"] == []
    assert transcript.json()["segment_count"] == 0


# --- migrations -------------------------------------------------------------


def test_a_failing_migration_leaves_the_database_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken migration must not damage the database it was applied to."""
    from app import db as db_module

    database = tmp_path / "clipforge.db"

    # A database in the shape the Phase 2 build left it in.
    conn = db_module.connect(database)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        for version, statements in db_module.MIGRATIONS[:2]:
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, "2026-10-20T00:00:00+00:00"),
            )
        conn.execute(
            """
            INSERT INTO jobs (
                id, source_type, source_reference, source_path, status, progress,
                stage, created_at, updated_at, attempt_count, original_filename,
                file_size_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "6" * 32,
                "upload",
                "old.mp4",
                "sources/old.mp4",
                "completed",
                100,
                "ready",
                "2026-10-20T00:00:00+00:00",
                "2026-10-20T00:00:01+00:00",
                1,
                "old.mp4",
                4096,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.close()

    original = db_module.MIGRATIONS
    broken = [
        (version, statements + ("THIS IS NOT SQL",))
        if version == 3
        else (version, statements)
        for version, statements in original
    ]
    monkeypatch.setattr(db_module, "MIGRATIONS", broken)

    with pytest.raises(sqlite3.Error):
        conn = db_module.connect(database)
        try:
            db_module.ensure_schema(conn)
        finally:
            conn.close()

    # The original database is intact and still readable.
    monkeypatch.setattr(db_module, "MIGRATIONS", original)
    conn = db_module.connect(database)
    try:
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        assert version == 2
        job = get_job(conn, "6" * 32)
        assert job is not None
        assert job.status is JobStatus.COMPLETED
        assert job.source_path == "sources/old.mp4"
        assert job.file_size_bytes == 4096
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    # And the migration still works afterwards.
    with session(database) as conn:
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        assert version == SCHEMA_VERSION
        job = get_job(conn, "6" * 32)
        assert job is not None
        assert job.status is JobStatus.COMPLETED


# --- the worker shuts down cleanly -------------------------------------------


def test_the_worker_leaves_the_database_readable_after_a_shutdown(
    settings: Settings,
) -> None:
    _queue_job(settings, "5" * 32)

    worker = Worker(settings, worker_id="w1", transcriber=FakeTranscriber())
    worker.request_stop()
    worker.run_forever()

    with session(settings.resolved_database_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        job = get_job(conn, "5" * 32)
        assert job is not None
        assert job.status is JobStatus.QUEUED
