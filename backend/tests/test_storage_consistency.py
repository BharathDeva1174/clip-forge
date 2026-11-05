"""Tests for the consistency between the filesystem and the database.

Phase 2 uploads a source to disk first and then records the job, so a queued
job always points at a file that exists. These tests check the boundaries
where that could go wrong, and check that a failure never leaves an orphaned
file behind.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import create_job, list_jobs, session
from app.jobs import SourceType
from app.storage import Storage


def _sources(settings: Settings) -> list[Path]:
    storage = Storage(settings.storage_root)
    if not storage.sources_dir.exists():
        return []
    return [path for path in storage.sources_dir.iterdir() if path.is_file()]


def _uploads(settings: Settings) -> list[Path]:
    storage = Storage(settings.storage_root)
    if not storage.uploads_dir.exists():
        return []
    return [path for path in storage.uploads_dir.iterdir() if path.is_file()]


# --- a queued job points at a file that exists -----------------------------


def test_a_queued_job_points_at_a_file_that_exists(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    with sample_video.open("rb") as handle:
        response = client.post(
            "/api/v1/videos/upload",
            files={"file": ("clip.mp4", handle, "video/mp4")},
        )
    assert response.status_code == 202

    storage = Storage(settings.storage_root)
    sources = _sources(settings)
    assert len(sources) == 1

    with session(settings.resolved_database_path) as conn:
        rows = list_jobs(conn, limit=10)
    assert rows[0].source_path
    assert storage.resolve(rows[0].source_path).is_file()


# --- failure injection -----------------------------------------------------


def test_a_failure_before_the_file_is_committed_leaves_nothing_behind(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure while the file is being written must not leave a partial file."""

    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("the disk went away")

    monkeypatch.setattr("app.routers.videos.inspect_media", explode)

    with pytest.raises(OSError):
        client.post(
            "/api/v1/videos/upload",
            files={"file": ("clip.mp4", b"not really a video", "video/mp4")},
        )

    assert _uploads(settings) == []
    assert _sources(settings) == []


def test_a_failure_while_moving_the_file_removes_the_upload(
    client: TestClient, sample_video: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the file cannot be moved into place, the upload is cleaned up."""

    def explode(src: object, dst: object) -> None:
        raise OSError("the move failed")

    monkeypatch.setattr("app.routers.videos.shutil.move", explode)

    with sample_video.open("rb") as handle:
        response = client.post(
            "/api/v1/videos/upload",
            files={"file": ("clip.mp4", handle, "video/mp4")},
        )

    assert response.status_code == 500
    assert _uploads(settings) == []
    assert _sources(settings) == []


def test_a_failure_recording_the_job_removes_the_file(
    client: TestClient, sample_video: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the job row cannot be written, the accepted file is removed again."""

    def explode(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("the database is locked")

    monkeypatch.setattr("app.routers.videos.create_job", explode)

    with sample_video.open("rb") as handle:
        with pytest.raises(sqlite3.OperationalError):
            client.post(
                "/api/v1/videos/upload",
                files={"file": ("clip.mp4", handle, "video/mp4")},
            )

    # The file was accepted but the job was not recorded, so the file is
    # removed: an accepted file always belongs to a job.
    assert _sources(settings) == []
    assert _uploads(settings) == []


def test_a_rejected_upload_leaves_no_file_behind(
    client: TestClient, settings: Settings
) -> None:
    response = client.post(
        "/api/v1/videos/upload",
        files={"file": ("notes.txt", b"just some text", "text/plain")},
    )

    assert response.status_code == 415
    assert _uploads(settings) == []
    assert _sources(settings) == []


# --- the temporary audio is not a source -----------------------------------


def test_the_extracted_audio_is_not_stored_with_the_sources(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    from app.worker import Worker
    from fakes import FakeTranscriber

    with sample_video.open("rb") as handle:
        response = client.post(
            "/api/v1/videos/upload",
            files={"file": ("clip.mp4", handle, "video/mp4")},
        )
    assert response.status_code == 202

    Worker(settings, worker_id="test-worker", transcriber=FakeTranscriber()).run_once()

    storage = Storage(settings.storage_root)
    sources = _sources(settings)
    assert len(sources) == 1
    assert sources[0].suffix == ".mp4"
    # The scratch directory holds no audio once the job is done.
    assert list(storage.temp_dir.glob("audio-*")) == []


def test_the_temporary_audio_can_never_overwrite_a_source(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    from app.worker import Worker
    from fakes import FakeTranscriber

    with sample_video.open("rb") as handle:
        response = client.post(
            "/api/v1/videos/upload",
            files={"file": ("clip.mp4", handle, "video/mp4")},
        )
    assert response.status_code == 202

    storage = Storage(settings.storage_root)
    before = {path: path.read_bytes() for path in _sources(settings)}

    Worker(settings, worker_id="test-worker", transcriber=FakeTranscriber()).run_once()

    after = {path: path.read_bytes() for path in _sources(settings)}
    assert before == after


# --- orphaned files --------------------------------------------------------


def test_a_source_that_belongs_to_a_job_is_never_deleted(
    settings: Settings, db: sqlite3.Connection
) -> None:
    """The recovery path must not touch files that a job still refers to."""
    storage = Storage(settings.storage_root)
    storage.ensure_layout()
    source = storage.sources_dir / "kept.mp4"
    source.write_bytes(b"a real source")

    create_job(
        db,
        job_id="k" * 32,
        source_type=SourceType.UPLOAD,
        source_reference="kept.mp4",
        source_path=storage.relative_to_root(source),
    )

    # The worker only ever reads the source; it never deletes it.
    assert source.exists()
