"""Tests for the video upload endpoint."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import get_job
from app.errors import ErrorCode
from app.main import create_app
from app.storage import Storage


def _upload(
    client: TestClient,
    path: Path,
    *,
    filename: str | None = None,
    content_type: str = "application/octet-stream",
):
    """Post a file to the upload endpoint."""
    with path.open("rb") as handle:
        return client.post(
            "/api/v1/videos/upload",
            files={"file": (filename or path.name, handle, content_type)},
        )


def _error_code(response) -> str:
    return response.json()["error"]["code"]


# --- the happy path ---------------------------------------------------------


def test_a_valid_video_is_accepted(client: TestClient, sample_video: Path) -> None:
    response = _upload(client, sample_video)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["stage"] == "queued"
    assert body["progress"] == 0
    assert body["source_type"] == "upload"
    assert body["error"] is None
    assert len(body["id"]) == 32


def test_the_job_is_persisted_in_sqlite(
    client: TestClient, sample_video: Path, db: sqlite3.Connection
) -> None:
    body = _upload(client, sample_video).json()

    job = get_job(db, body["id"])
    assert job is not None
    assert job.status.value == "queued"
    assert job.source_type.value == "upload"
    assert job.source_path is not None
    assert job.attempt_count == 0


def test_the_source_file_lives_inside_the_storage_root(
    client: TestClient, sample_video: Path, db: sqlite3.Connection, settings: Settings
) -> None:
    body = _upload(client, sample_video).json()
    job = get_job(db, body["id"])

    storage = Storage(settings.storage_root)
    resolved = storage.resolve(job.source_path)
    assert resolved.is_file()
    assert storage.contains(resolved)
    assert resolved.parent == storage.sources_dir


def test_the_job_response_does_not_leak_a_filesystem_path(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    response = _upload(client, sample_video)
    assert str(settings.storage_root) not in response.text
    assert "source_path" not in response.json()


# --- rejections -------------------------------------------------------------


def test_a_zero_byte_file_is_rejected(client: TestClient, tmp_path: Path) -> None:
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")

    response = _upload(client, empty)

    assert response.status_code == 400
    assert _error_code(response) == ErrorCode.EMPTY_FILE.value


def test_an_unsupported_extension_is_rejected(
    client: TestClient, sample_video: Path
) -> None:
    response = _upload(client, sample_video, filename="clip.txt")

    assert response.status_code == 415
    assert _error_code(response) == ErrorCode.UNSUPPORTED_MEDIA_TYPE.value


def test_a_misleading_extension_is_rejected_by_media_inspection(
    client: TestClient, not_a_video: Path
) -> None:
    # The file claims to be an MP4 but contains no video at all.
    response = _upload(client, not_a_video, content_type="video/mp4")

    assert response.status_code == 415
    assert _error_code(response) == ErrorCode.INVALID_MEDIA.value


def test_malformed_media_is_rejected(client: TestClient, tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 512)

    response = _upload(client, broken)

    assert response.status_code == 415
    assert _error_code(response) == ErrorCode.INVALID_MEDIA.value


def test_an_oversized_upload_is_rejected_with_413(
    storage_root: Path, sample_video: Path
) -> None:
    small = Settings(
        _env_file=None,
        storage_dir=storage_root,
        database_path=storage_root / "database" / "clipforge.db",
        max_upload_bytes=1024,
        log_level="WARNING",
    )
    with TestClient(create_app(small)) as client:
        response = _upload(client, sample_video)

    assert response.status_code == 413
    assert _error_code(response) == ErrorCode.PAYLOAD_TOO_LARGE.value


def test_an_oversized_body_is_rejected_before_it_is_read(
    storage_root: Path, sample_video: Path
) -> None:
    # A Content-Length above the limit is refused by the middleware, so the
    # body is never streamed into the application.
    small = Settings(
        _env_file=None,
        storage_dir=storage_root,
        database_path=storage_root / "database" / "clipforge.db",
        max_upload_bytes=1024,
        log_level="WARNING",
    )
    with TestClient(create_app(small)) as client:
        response = client.post(
            "/api/v1/videos/upload",
            files={"file": ("clip.mp4", b"x" * 4096, "video/mp4")},
        )

    assert response.status_code == 413
    assert _error_code(response) == ErrorCode.PAYLOAD_TOO_LARGE.value


def test_a_rejected_upload_leaves_no_partial_file_behind(
    client: TestClient, not_a_video: Path, settings: Settings
) -> None:
    storage = Storage(settings.storage_root)

    response = _upload(client, not_a_video)

    assert response.status_code == 415
    assert list(storage.uploads_dir.iterdir()) == []
    assert list(storage.sources_dir.iterdir()) == []


def test_path_traversal_filenames_cannot_escape_the_storage_directory(
    client: TestClient, sample_video: Path, settings: Settings, db: sqlite3.Connection
) -> None:
    storage = Storage(settings.storage_root)

    response = _upload(
        client, sample_video, filename="../../../../Windows/System32/evil.mp4"
    )

    assert response.status_code == 202
    job = get_job(db, response.json()["id"])
    # The stored path is server generated and resolves inside the root.
    resolved = storage.resolve(job.source_path)
    assert resolved.is_file()
    assert resolved.parent == storage.sources_dir
    assert storage.contains(resolved)
    assert not (storage.root.parent / "evil.mp4").exists()


def test_repeated_uploads_do_not_overwrite_each_other(
    client: TestClient, sample_video: Path, settings: Settings
) -> None:
    storage = Storage(settings.storage_root)

    first = _upload(client, sample_video).json()
    second = _upload(client, sample_video).json()

    assert first["id"] != second["id"]
    sources = sorted(storage.sources_dir.iterdir())
    assert len(sources) == 2
    assert sources[0].name != sources[1].name


def test_the_client_supplied_content_type_cannot_bypass_validation(
    client: TestClient, not_a_video: Path, sample_video: Path
) -> None:
    # A "video/mp4" content type on a file that is not a video is still
    # rejected.
    rejected = _upload(client, not_a_video, content_type="video/mp4")
    assert rejected.status_code == 415

    # A "text/plain" content type on a real video is still accepted: the
    # content type is never trusted in either direction.
    accepted = _upload(client, sample_video, content_type="text/plain")
    assert accepted.status_code == 202


def test_a_missing_ffprobe_produces_an_clear_error(
    storage_root: Path, sample_video: Path
) -> None:
    settings = Settings(
        _env_file=None,
        storage_dir=storage_root,
        database_path=storage_root / "database" / "clipforge.db",
        ffprobe_path="ffprobe-that-does-not-exist",
        log_level="WARNING",
    )
    with TestClient(create_app(settings)) as client:
        response = _upload(client, sample_video)

    assert response.status_code == 415
    assert _error_code(response) == ErrorCode.MEDIA_TOOL_UNAVAILABLE.value
    assert "ffprobe" in response.json()["error"]["message"].lower()


def test_a_request_without_a_multipart_body_is_rejected(client: TestClient) -> None:
    response = client.post("/api/v1/videos/upload")

    assert response.status_code == 415
    assert _error_code(response) == ErrorCode.UNSUPPORTED_MEDIA_TYPE.value


def test_a_non_multipart_body_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/videos/upload",
        content=b"not multipart",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 415
    assert _error_code(response) == ErrorCode.UNSUPPORTED_MEDIA_TYPE.value


def test_invalid_configuration_is_rejected_without_exposing_secrets() -> None:
    from pydantic import ValidationError

    secret = "super-secret-llm-key"
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, port="not-a-port", llm_api_key=secret)

    assert secret not in str(excinfo.value)
