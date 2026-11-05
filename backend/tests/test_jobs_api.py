"""Tests for the job listing and status endpoints."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import create_job, get_job, session
from app.errors import ErrorCode
from app.jobs import JobStage, JobStatus, SourceType, assert_transition, InvalidTransitionError


def _make_job(conn: sqlite3.Connection, job_id: str = "a" * 32) -> str:
    create_job(
        conn,
        job_id=job_id,
        source_type=SourceType.UPLOAD,
        source_reference="clip.mp4",
        source_path="sources/clip.mp4",
    )
    return job_id


def test_a_job_survives_closing_and_reopening_the_database(settings) -> None:
    job_id = "b" * 32
    with session(settings.resolved_database_path) as conn:
        _make_job(conn, job_id)

    # A completely new connection, as a restarted process would use.
    with session(settings.resolved_database_path) as conn:
        job = get_job(conn, job_id)

    assert job is not None
    assert job.id == job_id
    assert job.status is JobStatus.QUEUED


def test_an_unknown_job_id_returns_404(client: TestClient) -> None:
    response = client.get("/api/v1/jobs/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["status"] == 404
    assert body["error"]["code"] == ErrorCode.NOT_FOUND.value
    assert "message" in body["error"]


def test_the_job_list_is_ordered_newest_first(client: TestClient, db: sqlite3.Connection) -> None:
    for index in range(3):
        _make_job(db, f"{index:032x}")
        # Make the creation order unambiguous.
        db.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?",
            (f"2026-01-0{index + 1}T00:00:00+00:00", f"{index:032x}"),
        )

    items = client.get("/api/v1/jobs").json()["items"]

    assert [item["id"] for item in items] == [
        f"{2:032x}",
        f"{1:032x}",
        f"{0:032x}",
    ]


def test_the_job_list_is_bounded(client: TestClient, db: sqlite3.Connection) -> None:
    for index in range(10):
        _make_job(db, f"{index:032x}")

    body = client.get("/api/v1/jobs?limit=3").json()

    assert len(body["items"]) == 3
    assert body["limit"] == 3
    assert body["offset"] == 0
    assert body["total"] == 10


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=1000", "limit=-1", "offset=-1", "limit=abc"],
)
def test_invalid_pagination_values_are_rejected(client: TestClient, query: str) -> None:
    response = client.get(f"/api/v1/jobs?{query}")

    assert response.status_code == 422
    assert "error" in response.json()


def test_the_job_response_does_not_reveal_a_filesystem_path(
    client: TestClient, db: sqlite3.Connection, settings
) -> None:
    job_id = _make_job(db)

    response = client.get(f"/api/v1/jobs/{job_id}")

    assert response.status_code == 200
    assert "source_path" not in response.json()
    assert "sources/clip.mp4" not in response.text
    assert str(settings.storage_root) not in response.text


def test_the_job_response_does_not_reveal_worker_internals(
    client: TestClient, db: sqlite3.Connection
) -> None:
    job_id = _make_job(db)
    db.execute(
        "UPDATE jobs SET worker_id = 'secret-host-pid', lease_expires_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
        (job_id,),
    )

    body = client.get(f"/api/v1/jobs/{job_id}").json()

    assert "worker_id" not in body
    assert "lease_expires_at" not in body
    assert "secret-host-pid" not in str(body)


def test_a_job_response_carries_the_documented_fields(
    client: TestClient, db: sqlite3.Connection
) -> None:
    job_id = _make_job(db)

    body = client.get(f"/api/v1/jobs/{job_id}").json()

    for field in (
        "id",
        "source_type",
        "source_reference",
        "status",
        "progress",
        "stage",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "attempt_count",
        "error",
    ):
        assert field in body


def test_an_error_on_a_job_is_reported_with_a_stable_code(
    client: TestClient, db: sqlite3.Connection
) -> None:
    job_id = _make_job(db)
    db.execute(
        "UPDATE jobs SET status = ?, stage = ?, error_code = ?, error_message = ? WHERE id = ?",
        (
            JobStatus.FAILED.value,
            JobStage.FAILED.value,
            ErrorCode.INVALID_MEDIA.value,
            "The file could not be read as a media file.",
            job_id,
        ),
    )

    body = client.get(f"/api/v1/jobs/{job_id}").json()

    assert body["status"] == "failed"
    assert body["error"]["code"] == ErrorCode.INVALID_MEDIA.value
    assert body["error"]["message"]


def test_an_invalid_progress_value_cannot_be_stored(db: sqlite3.Connection) -> None:
    job_id = _make_job(db)

    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE jobs SET progress = 150 WHERE id = ?", (job_id,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE jobs SET progress = -5 WHERE id = ?", (job_id,))


def test_an_invalid_status_cannot_be_stored(db: sqlite3.Connection) -> None:
    job_id = _make_job(db)

    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE jobs SET status = 'nonsense' WHERE id = ?", (job_id,))


# --- state transitions ------------------------------------------------------


def test_a_completed_job_cannot_go_back_to_queued() -> None:
    with pytest.raises(InvalidTransitionError):
        assert_transition(JobStatus.COMPLETED, JobStatus.QUEUED)


def test_a_failed_job_cannot_be_requeued_by_an_ordinary_update() -> None:
    with pytest.raises(InvalidTransitionError):
        assert_transition(JobStatus.FAILED, JobStatus.QUEUED)


def test_a_cancelled_job_cannot_be_requeued() -> None:
    with pytest.raises(InvalidTransitionError):
        assert_transition(JobStatus.CANCELLED, JobStatus.QUEUED)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (JobStatus.QUEUED, JobStatus.VALIDATING),
        (JobStatus.QUEUED, JobStatus.DOWNLOADING),
        (JobStatus.VALIDATING, JobStatus.INSPECTING),
        (JobStatus.DOWNLOADING, JobStatus.INSPECTING),
        (JobStatus.INSPECTING, JobStatus.EXTRACTING_AUDIO),
        (JobStatus.EXTRACTING_AUDIO, JobStatus.TRANSCRIBING),
        (JobStatus.TRANSCRIBING, JobStatus.COMPLETED),
        (JobStatus.QUEUED, JobStatus.FAILED),
    ],
)
def test_the_allowed_transitions_are_permitted(
    current: JobStatus, new: JobStatus
) -> None:
    assert_transition(current, new)


def test_a_job_cannot_be_completed_without_being_transcribed() -> None:
    # Since Phase 3 a job has to pass through the transcription stages, so
    # `completed` always implies that a transcript was committed.
    with pytest.raises(InvalidTransitionError):
        assert_transition(JobStatus.INSPECTING, JobStatus.COMPLETED)


def test_an_illegal_transition_is_rejected_by_the_database_layer(
    db: sqlite3.Connection,
) -> None:
    from app.db import update_job

    job_id = _make_job(db)
    db.execute(
        "UPDATE jobs SET status = ?, stage = ? WHERE id = ?",
        (JobStatus.COMPLETED.value, JobStage.READY.value, job_id),
    )

    with pytest.raises(InvalidTransitionError):
        update_job(db, job_id, status=JobStatus.QUEUED)


def test_progress_never_decreases(db: sqlite3.Connection) -> None:
    from app.db import update_job

    job_id = _make_job(db)
    update_job(db, job_id, status=JobStatus.VALIDATING, progress=40)
    update_job(db, job_id, progress=10)

    job = get_job(db, job_id)
    assert job.progress == 40


def test_progress_is_always_within_the_allowed_range(db: sqlite3.Connection) -> None:
    from app.db import update_job

    job_id = _make_job(db)
    update_job(db, job_id, status=JobStatus.VALIDATING, progress=5000)

    job = get_job(db, job_id)
    assert 0 <= job.progress <= 100


# --- deletion ----------------------------------------------------------------


def _finish_job_tree(
    conn: sqlite3.Connection,
    storage_root,
    job_id: str = "c" * 32,
) -> dict[str, str]:
    """A completed job with a source file, a clip file and a format file.

    The clip and the format walk their legal transitions, the way the
    worker would. Returns the relative paths that were created.
    """
    from app.clips import ClipStatus
    from app.db import (
        create_clip,
        create_format,
        update_clip,
        update_format,
        update_job,
    )

    _make_job(conn, job_id)
    for status, stage in (
        (JobStatus.VALIDATING, JobStage.VALIDATING),
        (JobStatus.INSPECTING, JobStage.INSPECTING),
        (JobStatus.EXTRACTING_AUDIO, JobStage.EXTRACTING_AUDIO),
        (JobStatus.TRANSCRIBING, JobStage.TRANSCRIBING),
        (JobStatus.COMPLETED, JobStage.READY),
    ):
        update_job(conn, job_id, status=status, stage=stage)

    create_clip(
        conn,
        job_id=job_id,
        analysis_id=None,
        candidate_id=None,
        start_seconds=0.0,
        end_seconds=2.0,
        clip_id="e" * 32,
    )
    for status, stage, progress in (
        (ClipStatus.RENDERING, "rendering", 40),
        (ClipStatus.VERIFYING, "verifying", 85),
    ):
        update_clip(conn, "e" * 32, status=status, stage=stage, progress=progress)
    update_clip(
        conn,
        "e" * 32,
        status=ClipStatus.COMPLETED,
        stage="completed",
        progress=100,
        output_path="clips/clip-out.mp4",
        output_size_bytes=10,
        actual_duration_seconds=2.0,
        width=64,
        height=64,
        video_codec="h264",
        audio_codec="aac",
        clear_error=True,
    )

    create_format(
        conn,
        clip_id="e" * 32,
        job_id=job_id,
        config_fingerprint="fp",
        settings_json="{}",
        format_id="f" * 32,
    )
    for status, stage, progress in (
        (ClipStatus.RENDERING, "rendering", 40),
        (ClipStatus.VERIFYING, "verifying", 85),
    ):
        update_format(conn, "f" * 32, status=status, stage=stage, progress=progress)
    update_format(
        conn,
        "f" * 32,
        status=ClipStatus.COMPLETED,
        stage="completed",
        progress=100,
        output_path="formats/format-out.mp4",
        output_size_bytes=10,
        actual_duration_seconds=2.0,
        width=64,
        height=128,
        video_codec="h264",
        audio_codec="aac",
        clear_error=True,
    )

    paths = {}
    for relative in (
        "sources/clip.mp4",
        "clips/clip-out.mp4",
        "formats/format-out.mp4",
    ):
        file_path = storage_root / relative
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"x" * 16)
        paths[relative] = str(file_path)
    return paths


def test_a_finished_job_can_be_deleted_with_its_files(
    client: TestClient, db: sqlite3.Connection, settings
) -> None:
    from app.db import get_transcript, save_transcript
    from app.transcripts import Transcript

    files = _finish_job_tree(db, settings.storage_root)
    save_transcript(
        db,
        job_id="c" * 32,
        transcript=Transcript(
            language="en",
            model_name="tiny.en",
            audio_sample_rate=16000,
            audio_channels=1,
            duration_seconds=2.0,
            segments=(),
        ),
    )
    temp_root = settings.storage_root / "temp"
    (temp_root / f"yt-{'c' * 32}").mkdir(parents=True)
    (temp_root / f"yt-{'c' * 32}" / "part.mp4").write_bytes(b"p")
    (temp_root / f"format-{'f' * 32}-1").mkdir(parents=True)

    response = client.delete("/api/v1/jobs/" + "c" * 32)

    assert response.status_code == 204
    with session(settings.resolved_database_path) as conn:
        assert get_job(conn, "c" * 32) is None
        assert get_transcript(conn, "c" * 32) is None
        assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM clip_formats").fetchone()[0] == 0
    for file_path in files.values():
        assert not Path(file_path).exists()
    assert not (temp_root / f"yt-{'c' * 32}").exists()
    assert not (temp_root / f"format-{'f' * 32}-1").exists()


def test_a_job_that_has_not_finished_cannot_be_deleted(
    client: TestClient, db: sqlite3.Connection, settings
) -> None:
    _make_job(db)

    response = client.delete("/api/v1/jobs/" + "a" * 32)

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == ErrorCode.JOB_NOT_DELETABLE.value
    with session(settings.resolved_database_path) as conn:
        assert get_job(conn, "a" * 32) is not None


def test_a_cancelled_job_can_be_deleted(
    client: TestClient, db: sqlite3.Connection, settings
) -> None:
    from app.db import cancel_job

    _make_job(db)
    cancel_job(db, "a" * 32)

    response = client.delete("/api/v1/jobs/" + "a" * 32)

    assert response.status_code == 204
    with session(settings.resolved_database_path) as conn:
        assert get_job(conn, "a" * 32) is None


def test_deleting_a_missing_job_returns_404(client: TestClient) -> None:
    response = client.delete("/api/v1/jobs/" + "9" * 32)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.NOT_FOUND.value


def test_deletion_never_removes_a_path_outside_the_storage_root(
    client: TestClient, db: sqlite3.Connection, settings
) -> None:
    """An escaped path in the database is skipped, not followed."""
    from app.db import update_job

    _make_job(db)
    for status, stage in (
        (JobStatus.VALIDATING, JobStage.VALIDATING),
        (JobStatus.INSPECTING, JobStage.INSPECTING),
        (JobStatus.EXTRACTING_AUDIO, JobStage.EXTRACTING_AUDIO),
        (JobStatus.TRANSCRIBING, JobStage.TRANSCRIBING),
        (JobStatus.COMPLETED, JobStage.READY),
    ):
        update_job(db, "a" * 32, status=status, stage=stage)
    db.execute(
        "UPDATE jobs SET source_path = '../escaped.mp4' WHERE id = ?", ("a" * 32,)
    )
    outside = settings.storage_root.parent / "escaped.mp4"
    outside.write_bytes(b"keep me")

    response = client.delete("/api/v1/jobs/" + "a" * 32)

    assert response.status_code == 204
    assert outside.exists()
    outside.unlink()
